from datetime import datetime, date, timedelta
import time
from pathlib import Path
import warnings
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors

# --- CONFIGURATION & PATHS ---
if '__file__' in globals():
    WORKING_DIR = Path(__file__).resolve().parent
else:
    WORKING_DIR = Path.cwd()

# Try importing custom wattler tools with fallbacks
try:
    from wattler_tools import wdb, wattler_drive, wattler_chat 
    WATTLER_TOOLS_AVAILABLE = True
except ImportError:
    warnings.warn("Could not import wattler_tools. SQL, Drive, and Chat features will operate with mock fallbacks.")
    WATTLER_TOOLS_AVAILABLE = False
warnings.filterwarnings('ignore')

SERVER_TMP_DIR = Path('/home/waut/tmp')
BASE_DIR = SERVER_TMP_DIR if SERVER_TMP_DIR.is_dir() else WORKING_DIR

FOGAZ_PATH = BASE_DIR / "fogaz.csv"
ED_PATH = BASE_DIR / "ed.csv"
TIGAZ_PATH = BASE_DIR / "tigaz.xlsx"

# Google Drive Shared Folder ID containing the raw files
SHARED_DRIVE_FOLDER_ID = "1XHfnTEHt3GKgS8S-R2f0wcSpb8pP__yG"

CHAT_ID = "GAZ"

# Toggles
SEND_CHAT = True
UPLOAD_TO_DRIVE = True

# Filenames
SUMMARY_IMAGE_NAME = "alul_felul_nominalas.png"

OUTPUT_ORAS_POD = BASE_DIR / "oras_pod.csv"
OUTPUT_NOMINALT = BASE_DIR / "nominalt.csv"
OUTPUT_PORTFOLIO = BASE_DIR / "portfolio.csv"

# Target gas day is always D-1 (yesterday)
TARGET_GAS_DAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# Retry settings: 5 minutes wait between checks
RETRY_DELAY_SECONDS = 300  # 5 minutes
MAX_RETRIES = 1  # Number of retries before falling back completely


def download_raw_file_from_drive(file_pattern: str, local_save_path: Path, folder_id: str) -> bool:
    """
    Searches for a file matching `file_pattern` in the Google Drive folder,
    downloads its content, and writes it to `local_save_path`.
    """
    try:
        found_files = wattler_drive.get_file_ids_from_google_drive(
            file_name_pattern=file_pattern,
            google_folder_id=folder_id,
            search_subfolders=False
        )
        
        if not found_files:
            print(f"[DRIVE WARNING] No file matching '{file_pattern}' found in folder {folder_id}.")
            return False

        file_id = found_files[0]['id']
        file_name = found_files[0]['name']
        print(f"[DRIVE INFO] Downloading '{file_name}' (ID: {file_id})...")

        file_bytes = wattler_drive.get_file_content(file_id)
        if file_bytes is None:
            print(f"[DRIVE ERROR] Failed to retrieve content for file ID: {file_id}")
            return False

        local_save_path.write_bytes(file_bytes)
        print(f"[DRIVE SUCCESS] Downloaded and saved '{file_name}' to {local_save_path}")
        return True

    except Exception as e:
        print(f"[DRIVE ERROR] Exception occurred while fetching '{file_pattern}': {e}")
        return False


# --- HELPER FUNCTIONS ---
def parse_gasday_from_interval_start(dt_series: pd.Series) -> pd.Series:
    """For FŐGÁZ / ÉD ('Gáznap -tól'): Timestamps mark the START of the hour."""
    dt_parsed = pd.to_datetime(
        dt_series.astype(str).str.strip().str.replace(".", "-", regex=False),
        errors="coerce",
    )
    calendar_dates = dt_parsed.dt.date
    hours = dt_parsed.dt.hour
    is_early = hours.isin([0, 1, 2, 3, 4, 5])

    gas_days = [
        (d - timedelta(days=1)).strftime("%Y-%m-%d") if early else d.strftime("%Y-%m-%d")
        if pd.notna(d) else None
        for d, early in zip(calendar_dates, is_early)
    ]
    return pd.Series(gas_days, index=dt_series.index)


def parse_gasday_from_interval_end(dt_series: pd.Series) -> pd.Series:
    """For TIGÁZ ('Időbélyeg'): Timestamps mark the END of the hour."""
    dt_parsed = pd.to_datetime(
        dt_series.astype(str).str.strip().str.replace(".", "-", regex=False),
        errors="coerce",
    )
    calendar_dates = dt_parsed.dt.date
    hours = dt_parsed.dt.hour
    is_early = hours.isin([0, 1, 2, 3, 4, 5, 6])

    gas_days = [
        (d - timedelta(days=1)).strftime("%Y-%m-%d") if early else d.strftime("%Y-%m-%d")
        if pd.notna(d) else None
        for d, early in zip(calendar_dates, is_early)
    ]
    return pd.Series(gas_days, index=dt_series.index)


def load_csv_with_encoding(file_path: Path) -> pd.DataFrame:
    """Helper to read CSV files with encoding fallbacks."""
    try:
        return pd.read_csv(file_path, sep=None, engine="python", encoding="utf-8-sig")
    except Exception:
        return pd.read_csv(file_path, sep=None, engine="python", encoding="iso-8859-2")


def clean_numeric_column(series: pd.Series) -> pd.Series:
    """Cleans spaces, non-breaking spaces, and commas to prevent conversion drops."""
    return pd.to_numeric(
        series.astype(str)
        .str.replace("\xa0", "", regex=False)
        .str.replace(" ", "", regex=False)
        .str.replace(",", ".", regex=False),
        errors="coerce",
    )


def load_and_filter_standard_csv(file_path: Path, source_name: str, folder_id: str) -> pd.DataFrame:
    """Downloads raw CSV from Google Drive, parses standard columns, and filters by target gas day."""
    file_pattern = file_path.name
    download_success = download_raw_file_from_drive(file_pattern, file_path, folder_id)

    if not download_success or not file_path.exists():
        print(f"[{source_name.upper()}] Raw file missing or failed to download.")
        return pd.DataFrame(columns=["POD", "fogyasztas_m3"])

    df = load_csv_with_encoding(file_path)
    df.columns = df.columns.astype(str).str.strip()

    target_col = [c for c in df.columns if "Gáznap" in c and "tól" in c][0]
    df["calculated_gasday"] = parse_gasday_from_interval_start(df[target_col])

    valid_df = df[df["calculated_gasday"] == TARGET_GAS_DAY].copy()

    if valid_df.empty:
        print(f"[{source_name.upper()}] No valid rows for Gas Day {TARGET_GAS_DAY}.")
        return pd.DataFrame(columns=["POD", "fogyasztas_m3"])

    korrigalt_col = [c for c in valid_df.columns if "Korrigált fogyasztás" in c][0]
    uzemi_col = [c for c in valid_df.columns if "Üzemi fogyasztás" in c][0]
    pod_col = [c for c in valid_df.columns if "POD" in c][0]
    gyari_col = [c for c in valid_df.columns if "Gyáriszám" in c or "gyariszam" in c.lower()][0]
    adatpotlas_cols = [c for c in valid_df.columns if "Adatpótlás" in c or "adatpotlas" in c.lower()]

    raw_korr = valid_df[korrigalt_col].astype(str).str.strip()
    is_empty_korr = valid_df[korrigalt_col].isna() | (raw_korr == "") | (raw_korr.str.lower() == "nan")

    korr_vals = clean_numeric_column(valid_df[korrigalt_col])
    uzemi_vals = clean_numeric_column(valid_df[uzemi_col])

    valid_df["raw_m3"] = np.where(is_empty_korr, uzemi_vals, korr_vals)
    valid_df["raw_m3"] = valid_df["raw_m3"].fillna(0)
    valid_df["POD"] = valid_df[pod_col].astype(str).str.strip()

    if adatpotlas_cols:
        adat_col = adatpotlas_cols[0]
        is_nem = valid_df[adat_col].astype(str).str.strip().str.lower() == "nem"
        valid_df["is_nem"] = is_nem

        # Compute mean of measured ('nem') rows grouped by Gyáriszám
        valid_nem_df = valid_df[is_nem]
        nem_means = valid_nem_df.groupby(gyari_col)["raw_m3"].mean()
        mapped_means = valid_df[gyari_col].map(nem_means)

        # Decide which PODs are fully imputed at the GYÁRISZÁM-group level.
        # For each (POD, Gyáriszám) determine if there exists any measured ('nem') row.
        grp_has_measured = (
            valid_df.groupby(["POD", gyari_col])["is_nem"].any().reset_index(name="has_measured")
        )

        # For each POD, check if any of its Gyáriszám groups have measured rows.
        pod_has_any_measured = (
            grp_has_measured.groupby("POD")["has_measured"].any()
        )

        # PODs to drop are those with NO measured rows in any Gyáriszám group
        fully_imputed_pods = pod_has_any_measured[~pod_has_any_measured].index.tolist()
        if fully_imputed_pods:
            print(f"[{source_name.upper()}] Dropped {len(fully_imputed_pods)} POD(s) with 100% 'igen' rows across all Gyáriszám groups: {fully_imputed_pods}")
            valid_df = valid_df[~valid_df["POD"].isin(fully_imputed_pods)].copy()

        # For remaining pods: recompute mapped means after any row drops so indices align,
        # then replace imputed rows with the Gyáriszám mean when available. If a group has
        # no measured mean, fill with 0 so the POD still contributes via other groups.
        mapped_means = valid_df[gyari_col].map(nem_means).fillna(0)
        valid_df["fogyasztas_m3"] = np.where(valid_df["is_nem"], valid_df["raw_m3"], mapped_means)
    else:
        valid_df["fogyasztas_m3"] = valid_df["raw_m3"]

    if valid_df.empty:
        return pd.DataFrame(columns=["POD", "fogyasztas_m3"])

    out_df = valid_df.groupby("POD", as_index=False)["fogyasztas_m3"].sum()
    print(f"[{source_name.upper()}] Loaded {len(out_df)} valid távmért PODs for Gas Day: {TARGET_GAS_DAY}")
    return out_df


def load_and_filter_tigaz_xlsx(file_path: Path, folder_id: str) -> pd.DataFrame:
    """Downloads raw Tigáz XLSX from Google Drive, calculates daily volumes, and aggregates per POD."""
    file_pattern = file_path.name
    download_success = download_raw_file_from_drive(file_pattern, file_path, folder_id)

    if not download_success or not file_path.exists():
        print(f"[TIGAZ] Raw file missing or failed to download.")
        return pd.DataFrame(columns=["POD", "fogyasztas_m3"])

    df = pd.read_excel(file_path, engine="openpyxl")
    df.columns = df.columns.astype(str).str.strip()

    df["calculated_gasday"] = parse_gasday_from_interval_end(df["Időbélyeg"])
    valid_df = df[df["calculated_gasday"] == TARGET_GAS_DAY].copy()

    if valid_df.empty:
        print(f"[TIGAZ] No valid rows for Gas Day {TARGET_GAS_DAY}.")
        return pd.DataFrame(columns=["POD", "fogyasztas_m3"])

    g_vals = clean_numeric_column(valid_df["Órás korrigált számlálóállás"]).fillna(0)
    h_vals = clean_numeric_column(valid_df["Órás korrigált fogyasztás"])
    i_vals = clean_numeric_column(valid_df["Órás üzemi fogyasztás"])

    cond_both_zero = (g_vals == 0) & (h_vals == 0)

    fogyasztas_m3 = pd.Series(index=valid_df.index, dtype=float)
    fogyasztas_m3[cond_both_zero] = i_vals[cond_both_zero]
    fogyasztas_m3[~cond_both_zero] = h_vals[~cond_both_zero].combine_first(
        i_vals[~cond_both_zero]
    )

    pod_series = valid_df["Deregulációs mérési pont"].astype(str).str.strip()

    temp_df = pd.DataFrame({
        "POD": pod_series,
        "fogyasztas_m3": fogyasztas_m3.fillna(0),
    })

    out_df = temp_df.groupby("POD", as_index=False)["fogyasztas_m3"].sum()
    print(f"[TIGAZ] Loaded {len(out_df)} unique PODs for Gas Day: {TARGET_GAS_DAY}")
    return out_df


def fetch_pod_mapping() -> pd.DataFrame:
    engine = wdb.get_sqla_engine()
    sql_pod = """
    SELECT DISTINCT 
        CAST(id_POD AS CHAR) AS id_pod, 
        CAST(POD_azonosito AS CHAR) AS POD,
        namecpty_contr AS cegnev
    FROM 
        W2_sites_pods.POD_list_m0
    """
    with engine.connect() as conn:
        return pd.read_sql(sql_pod, con=conn)


def fetch_multipliers() -> tuple:
    engine = wdb.get_sqla_engine()

    # Try previous months (-1, -2, ..., -12) until we find conversion multipliers.
    # If none are found, return an empty DataFrame and let the caller apply a global fallback.
    max_lookback_months = 12
    for months_back in range(1, max_lookback_months + 1):
        sql_multipliers = f"""
        SELECT DISTINCT
            CAST(pg.POD_azonosito AS CHAR) AS POD,
            ca.gcv_conv_15fok AS szorzo
        FROM
            W2_sites_pods.POD_gas AS pg
        JOIN
            W7_gas_alloc.conversionvalue AS ca
        ON
            pg.id_kiadpont = ca.id_kiadp
        WHERE
            ca.gasmonth = DATE_FORMAT(LAST_DAY(CURDATE() - INTERVAL {months_back} MONTH), '%Y-%m-01')
        ORDER BY POD
        """
        with engine.connect() as conn:
            df = pd.read_sql(sql_multipliers, con=conn)

        if df is not None and not df.empty:
            print(f"[MULTIPLIER INFO] Using multipliers from {months_back} month(s) ago")
            return df, months_back

    print(f"[MULTIPLIER WARN] No multipliers found in last {max_lookback_months} months; caller should fallback to global default")
    return pd.DataFrame(columns=["POD", "szorzo"]), 0


def fetch_nominated_volumes(target_gas_day: str) -> pd.DataFrame:
    engine = wdb.get_sqla_engine()
    sql_nom = f"""
    SELECT 
        CAST(idpod AS CHAR) AS id_pod,
        gasday,
        nomvol_kwh,
        nomvol_sorsz,
        nommax_kwh,
        nommax_sorsz
    FROM 
        W7_gas_alloc.nom_pod_last 
    WHERE 
        gasday = '{target_gas_day}'
        AND (nomvol_sorsz <> 0 OR nomvol_kwh <> 0 OR nommax_kwh <> 0)
    """
    with engine.connect() as conn:
        return pd.read_sql(sql_nom, con=conn)


def fetch_a_long_data(target_gas_day: str) -> dict:
    engine = wdb.get_sqla_engine()
    sql_a_long = f"""
    SELECT 
        gasday, 
        store_MFGT, 
        lpfs_carry, 
        SUM_LONG 
    FROM 
        W7_gas_alloc.a_long
    WHERE 
        gasday = '{target_gas_day}'
    """
    with engine.connect() as conn:
        df_long = pd.read_sql(sql_a_long, con=conn)

    if df_long.empty:
        raise ValueError(f"No records found in W7_gas_alloc.a_long for gasday: {target_gas_day}")

    row = df_long.iloc[0]
    return {
        "store_MFGT": float(row["store_MFGT"] or 0),
        "lpfs_carry": float(row["lpfs_carry"] or 0),
        "SUM_LONG": float(row["SUM_LONG"] or 0),
    }


def fetch_nominated_origin_vol(target_gas_day: str) -> float:
    engine = wdb.get_sqla_engine()
    sql_nom_origin = f"""
    SELECT 
        SUM(nomvol_kwh) AS vol_kwh_pf
    FROM 
        W7_gas_alloc.`nom_last-1`
    WHERE 
        ifentryexit_exit = 1 
        AND id_kiadpont NOT IN (13, 345)
        AND gasday = '{target_gas_day}'
    """
    with engine.connect() as conn:
        df_res = pd.read_sql(sql_nom_origin, con=conn)

    vol_val = df_res["vol_kwh_pf"].iloc[0] if not df_res.empty else None
    return float(vol_val) if pd.notna(vol_val) else 0.0


def fetch_all_files_with_retry() -> tuple[dict[str, pd.DataFrame], list[str], list[str]]:
    """
    Checks drive for all 3 sources. If NO files are found with target gas day data,
    sends a chat alert, waits 10 minutes, and retries.
    
    Returns:
        sources_dict: dict of source_name -> DataFrame
        found_sources: list of source names that had valid data
        missing_sources: list of source names that were missing or had no data
    """
    sources_to_check = [
        ("fogaz", FOGAZ_PATH, "standard"),
        ("ed", ED_PATH, "standard"),
        ("tigaz", TIGAZ_PATH, "tigaz"),
    ]

    for attempt in range(MAX_RETRIES + 1):
        sources_dict = {}
        found_sources = []
        missing_sources = []

        for name, path, file_type in sources_to_check:
            if file_type == "standard":
                df = load_and_filter_standard_csv(path, name, SHARED_DRIVE_FOLDER_ID)
            else:
                df = load_and_filter_tigaz_xlsx(path, SHARED_DRIVE_FOLDER_ID)

            sources_dict[name] = df
            if not df.empty:
                found_sources.append(name.upper())
            else:
                missing_sources.append(name.upper())

        # Check if ALL 3 files were found
        all_files_present = len(missing_sources) == 0

        # Proceed ONLY if all files exist OR we finished our retry attempt
        if all_files_present or attempt == MAX_RETRIES:
            return sources_dict, found_sources, missing_sources

        # Missing files detected; send message with exact missing files, wait, then retry
        retry_minutes = RETRY_DELAY_SECONDS // 60
        missing_str = ", ".join(missing_sources)
        wait_msg = f"*DATA MISSING*: Missing file(s) for Gas Day `{TARGET_GAS_DAY}`: `{missing_str}`. Retrying in {retry_minutes} minutes..."
        print(f"[TIME RELAY] {wait_msg}")
        if SEND_CHAT:
            try:
                wattler_chat.send_chat(CHAT_ID, wait_msg)
            except Exception as e:
                print(f"[CHAT ERROR] Failed to send chat message: {e}")
        else:
            print("[CHAT DISABLED] Skipping send_chat for missing files")

        time.sleep(RETRY_DELAY_SECONDS)

    return sources_dict, found_sources, missing_sources

def calculate_mvm_imputed_ratios(fogaz_path: Path) -> dict:
    """
    Reads the raw MVM/FŐGÁZ CSV and builds a lookup mapping POD ID string -> Imputation Ratio (float).
    Replicates Excel: COUNTIFS(..., "igen") / MAX(COUNTIFS(...), 1)
    """
    if not fogaz_path.exists():
        return {}

    try:
        df = load_csv_with_encoding(fogaz_path)
        df.columns = df.columns.astype(str).str.strip()

        # Find POD and Adatpótlás columns
        pod_cols = [c for c in df.columns if "POD" in c]
        adatpotlas_cols = [c for c in df.columns if "Adatpótlás" in c or "adatpotlas" in c.lower()]

        if not pod_cols or not adatpotlas_cols:
            return {}

        pod_col = pod_cols[0]
        adat_col = adatpotlas_cols[0]

        df["POD_clean"] = df[pod_col].astype(str).str.strip()
        df["is_imputed"] = df[adat_col].astype(str).str.strip().str.lower() == "igen"

        # Calculate counts per POD string
        stats = df.groupby("POD_clean").agg(
            total_count=("is_imputed", "count"),
            imputed_count=("is_imputed", "sum")
        )

        stats["ratio"] = stats["imputed_count"] / np.maximum(stats["total_count"], 1)
        return stats["ratio"].to_dict()

    except Exception as e:
        print(f"[IMPUTATION WARN] Failed to calculate MVM imputation ratios: {e}")
        return {}


def get_excel_imputation_string(pod_str: str, mvm_ratios: dict) -> str:
    """
    Replicates Excel:
    =IF(MID(POD,4,2)="11", "-", COUNTIFS('MVM'!C:C, POD, 'MVM'!I:I, "igen") / MAX(COUNTIFS('MVM'!C:C, POD), 1))
    """
    pod_str = str(pod_str).strip()

    # 1. Check MID(POD, 4, 2) == "11" (ÉD check)
    if len(pod_str) >= 5 and pod_str[3:5] == "11":
        return "-"

    # 2. If POD is in MVM export data, return formatted percentage
    if pod_str in mvm_ratios:
        ratio = mvm_ratios[pod_str]
        return f"{ratio * 100:.1f}%"

    # 3. If POD is not in MVM (e.g. Tigáz), Excel COUNTIFS returns 0 / MAX(0, 1) = 0.0%
    return "0.0%"


def calculate_mvm_imputed_ratios(file_paths: list[Path], target_gas_day: str) -> dict:
    """
    Reads MVM exports (FŐGÁZ + ÉD CSVs), filters for TARGET_GAS_DAY,
    and calculates a combined POD ID -> Imputation Ratio dictionary.
    """
    all_dfs = []

    for path in file_paths:
        if not path or not path.exists():
            continue

        try:
            df = load_csv_with_encoding(path)
            df.columns = df.columns.astype(str).str.strip()

            # Filter for target gas day
            target_col_candidates = [c for c in df.columns if "Gáznap" in c and "tól" in c]
            if target_col_candidates:
                df["calculated_gasday"] = parse_gasday_from_interval_start(df[target_col_candidates[0]])
                df = df[df["calculated_gasday"] == target_gas_day].copy()

            if not df.empty:
                all_dfs.append(df)

        except Exception as e:
            print(f"[IMPUTATION WARN] Failed to load {path.name}: {e}")

    if not all_dfs:
        print("[IMPUTATION WARN] No MVM data loaded for ratio calculation.")
        return {}

    # Concatenate FŐGÁZ and ÉD dataframes
    combined_df = pd.concat(all_dfs, ignore_index=True)

    pod_cols = [c for c in combined_df.columns if "POD" in c]
    adatpotlas_cols = [c for c in combined_df.columns if "Adatpótlás" in c or "adatpotlas" in c.lower()]

    if not pod_cols or not adatpotlas_cols:
        return {}

    pod_col = pod_cols[0]
    adat_col = adatpotlas_cols[0]

    combined_df["POD_clean"] = combined_df[pod_col].astype(str).str.strip().str.upper()
    combined_df["is_imputed"] = combined_df[adat_col].astype(str).str.strip().str.lower() == "igen"

    # Calculate ratios per POD
    stats = combined_df.groupby("POD_clean").agg(
        total_count=("is_imputed", "count"),
        imputed_count=("is_imputed", "sum")
    )

    stats["ratio"] = stats["imputed_count"] / np.maximum(stats["total_count"], 1)
    return stats["ratio"].to_dict()


def get_excel_imputation_string(pod_str: str, mvm_ratios: dict) -> str:
    pod_str = str(pod_str).strip().upper()

    # 1. Tigáz PODs (39N11...) do not have MVM imputation exports
    if pod_str.startswith("39N11"):
        return "-"

    # 2. Look up in combined MVM (FŐGÁZ 39N06 + ÉD 39N05) ratios
    if pod_str in mvm_ratios:
        ratio = mvm_ratios[pod_str]
        return f"{ratio * 100:.1f}%"

    # 3. Fallback for non-MVM or missing PODs
    return "-"


def generate_top_bottom_elteres_table(portfolio_df: pd.DataFrame, pod_map_df: pd.DataFrame, fogaz_path: Path, ed_path: Path) -> pd.DataFrame:
    """
    Generates a 10-row side-by-side Top 10 (túlnominálás) and Bottom 10 (alulnominálás) summary table.
    """
    # 1. Merge POD string and company name into portfolio using pod_map_df
    df = pd.merge(portfolio_df, pod_map_df[["id_pod", "POD", "cegnev"]], on="id_pod", how="left")
    df["cegnev"] = df["cegnev"].fillna("Ismeretlen")

    # 2. Calculate Eltérés (Nominated - Export/Fogyasztás)
    df["eltereskwh"] = np.where(
        df["source"] == "tavmert",
        df["nomvol_kwh"] - df["total_kwh_fogyasztas"],
        np.nan
    )

    valid_diffs = df.dropna(subset=["eltereskwh"]).copy()
    if valid_diffs.empty:
        print("[SUMMARY WARN] No valid távmért differences available for Top/Bottom table.")
        return pd.DataFrame()

    # 3. Calculate MVM Imputation Ratios dictionary
    mvm_ratios = calculate_mvm_imputed_ratios([fogaz_path, ed_path], TARGET_GAS_DAY)

    # 4. Apply Excel formula logic to get exact string
    valid_diffs["potolt_adatok_aranya"] = valid_diffs["POD"].apply(
        lambda pod: get_excel_imputation_string(pod, mvm_ratios)
    )

    valid_diffs["tulnominalas"] = valid_diffs["eltereskwh"].round().astype(int)

    # 5. Top 10 (Túlnominálás) & Bottom 10 (Alulnominálás)
    top_10 = (
        valid_diffs.nlargest(10, "tulnominalas")[["tulnominalas", "cegnev", "potolt_adatok_aranya"]]
        .reset_index(drop=True)
    )

    bottom_10 = (
        valid_diffs.nsmallest(10, "tulnominalas")[["tulnominalas", "cegnev", "potolt_adatok_aranya"]]
        .reset_index(drop=True)
    )
    bottom_10.rename(columns={"tulnominalas": "alulnominalas"}, inplace=True)

    # 6. Pad empty rows if fewer than 10 rows exist
    for idx in range(len(top_10), 10):
        top_10.loc[idx] = ["", "", ""]
    for idx in range(len(bottom_10), 10):
        bottom_10.loc[idx] = ["", "", ""]

    # 7. Build Side-by-Side Summary DataFrame
    summary_table = pd.DataFrame({
        "túlnominálás": top_10["tulnominalas"],
        "cégnév (túl)": top_10["cegnev"],
        "pótolt adatok aránya (túl)": top_10["potolt_adatok_aranya"],
        "alulnominálás": bottom_10["alulnominalas"],
        "cégnév (alul)": bottom_10["cegnev"],
        "pótolt adatok aránya (alul)": bottom_10["potolt_adatok_aranya"],
    })

    return summary_table

def export_styled_summary_image(summary_table: pd.DataFrame, output_path: str | Path | None = None) -> str:
    """
    Renders the Top/Bottom 10 summary dataframe into a beautifully formatted PNG image
    with color gradients for nominations and imputation ratios.
    """
    if summary_table.empty:
        print("[WARN] Summary table is empty. Skipping image export.")
        return ""

    df = summary_table.copy()
    rows, cols = df.shape

    # 1. Setup figure dimensions and axis
    fig, ax = plt.subplots(figsize=(14, 0.6 * (rows + 1.5)))
    ax.axis("off")

    # 2. Extract and parse numeric values for color mapping
    def parse_int(val):
        try:
            return abs(int(val))
        except (ValueError, TypeError):
            return 0

    def parse_pct(val):
        try:
            val_str = str(val).replace("%", "").strip()
            return float(val_str)
        except (ValueError, TypeError):
            return 0.0

    tul_vals = [parse_int(x) for x in df["túlnominálás"]]
    alul_vals = [parse_int(x) for x in df["alulnominálás"]]
    pct_tul_vals = [parse_pct(x) for x in df["pótolt adatok aránya (túl)"]]
    pct_alul_vals = [parse_pct(x) for x in df["pótolt adatok aránya (alul)"]]

    max_tul = max(tul_vals) if max(tul_vals) > 0 else 1
    max_alul = max(alul_vals) if max(alul_vals) > 0 else 1
    max_pct = max(max(pct_tul_vals), max(pct_alul_vals))
    max_pct = max_pct if max_pct > 0 else 100.0

    # Color Maps
    cmap_red = plt.cm.Reds      # Túlnominálás (Over)
    cmap_blue = plt.cm.Blues    # Alulnominálás (Under)
    cmap_orange = plt.cm.YlOrRd # Pótolt adatok (Imputation)

    # 3. Build cell color matrix
    cell_colors = []
    for r in range(rows):
        row_colors = []
        for c, col_name in enumerate(df.columns):
            val = df.iloc[r, c]

            # Alternating background default (light zebra striping)
            default_bg = "#f9f9f9" if r % 2 == 0 else "#ffffff"

            if col_name == "túlnominálás" and val != "":
                norm = parse_int(val) / max_tul
                # Gentle red tint scale
                color = mcolors.to_hex(cmap_red(0.1 + 0.5 * norm))
            elif col_name == "alulnominálás" and val != "":
                norm = parse_int(val) / max_alul
                # Gentle blue tint scale
                color = mcolors.to_hex(cmap_blue(0.1 + 0.5 * norm))
            elif "pótolt adatok aránya" in col_name and val not in ["-", "0.0%", ""]:
                pct = parse_pct(val)
                norm = pct / max_pct
                # Soft yellow-orange tint scale for non-zero ratios
                color = mcolors.to_hex(cmap_orange(0.15 + 0.5 * norm))
            else:
                color = default_bg

            row_colors.append(color)
        cell_colors.append(row_colors)

    # 4. Render Table
    table = ax.table(
        cellText=df.values,
        colLabels=df.columns,
        cellColours=cell_colors,
        cellLoc="center",
        loc="center"
    )

    # 5. Fine-tune Styling & Fonts
    table.auto_set_font_size(False)
    table.set_fontsize(9.5)
    table.scale(1.0, 1.8)  # Increase vertical cell padding

    # Header Row Styling
    header_color_tul = "#d9534f"   # Deep Red Header
    header_color_alul = "#0275d8"  # Deep Blue Header

    for c, col_name in enumerate(df.columns):
        cell = table[0, c]
        cell.set_text_props(weight="bold", color="white")
        cell.set_height(0.08)

        if "túl" in col_name:
            cell.set_facecolor(header_color_tul)
        else:
            cell.set_facecolor(header_color_alul)

    # Make data text bold where appropriate
    for r in range(rows):
        for c in range(cols):
            cell = table[r + 1, c]
            val = df.iloc[r, c]
            if c in [0, 3]: # Nominations columns
                cell.get_text().set_weight("bold")

    plt.title("TOP 10 TÚLNOMINÁLÁS & ALULNOMINÁLÁS SUMMARY", fontsize=12, fontweight="bold", pad=15)
    plt.tight_layout()

    # 6. Save Image
    # Resolve output path: default to BASE_DIR/<filename> when None or relative string provided
    if output_path is None:
        file_path = BASE_DIR / SUMMARY_IMAGE_NAME
    else:
        file_path = Path(output_path)
        if not file_path.is_absolute():
            file_path = BASE_DIR / file_path

    # Ensure parent directory exists
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    plt.savefig(file_path, dpi=250, bbox_inches="tight")
    plt.close(fig)
    print(f"[SUCCESS] Summary image saved to: {file_path}")
    return str(file_path)


def helper_upload_to_drive(filename: str) -> str | None:
    """Upload a file to Google Drive and return a view URL (or None).

    Uses `wattler_drive.upload_file_to_google_drive` which may return True/False
    or an ID/dict depending on implementation. To reliably obtain a Drive link
    we upload then lookup the file ID in the target folder.
    """
    if not (UPLOAD_TO_DRIVE and WATTLER_TOOLS_AVAILABLE):
        return None
    try:
        # Normalize path
        file_path = Path(filename)
        if not file_path.is_absolute():
            file_path = BASE_DIR / filename

        if not file_path.exists():
            print(f" -> [DRIVE ERROR] File to upload not found: {file_path}")
            return None

        print(f" -> [DRIVE INFO] Uploading file to Drive: {file_path}")
        res = wattler_drive.upload_file_to_google_drive(str(file_path), SHARED_DRIVE_FOLDER_ID, overwrite=True, create_new_if_exists=False)
        print(f" -> [DRIVE DEBUG] Upload response: {res}")

        # If the upload helper returns a boolean True/False, we need to locate the file id
        if isinstance(res, bool):
            if not res:
                print(f" -> [DRIVE ERROR] Upload reported failure for: {file_path}")
                return None
            # Upload succeeded; try to resolve the file ID by name
            try:
                found = wattler_drive.get_file_ids_from_google_drive(file_name_pattern=file_path.name, google_folder_id=SHARED_DRIVE_FOLDER_ID, search_subfolders=False)
                if not found:
                    # fallback search by stem in case upload created a suffixed name
                    found = wattler_drive.get_file_ids_from_google_drive(file_name_pattern=file_path.stem, google_folder_id=SHARED_DRIVE_FOLDER_ID, search_subfolders=False)
                if found:
                    file_id = found[0].get("id")
                    print(f" -> [DRIVE INFO] Resolved uploaded file ID: {file_id}")
                    return f"https://drive.google.com/file/d/{file_id}/view"
                else:
                    print(f" -> [DRIVE WARN] Upload succeeded but could not locate file id for: {file_path.name}")
                    return None
            except Exception as e:
                print(f" -> [DRIVE ERROR] Could not resolve uploaded file id: {e}")
                return None

        # If upload returned dict or string-like id, handle those as well
        if isinstance(res, dict):
            file_id = res.get("id")
            if file_id:
                return f"https://drive.google.com/file/d/{file_id}/view"
        if isinstance(res, str):
            # assume it's a file id
            return f"https://drive.google.com/file/d/{res}/view"

        print(f" -> [DRIVE WARN] Upload returned unexpected response: {res}")
        return None
    except Exception as e:
        print(f" -> [DRIVE ERROR] Upload failed for {filename}: {e}")
        return None

# --- MAIN EXECUTION ---
def main():
    print(f"Processing data for Gas Day: {TARGET_GAS_DAY}\n")

    # 1. RETRY / FETCH FILES FROM DRIVE
    sources_dict, found_sources, missing_sources = fetch_all_files_with_retry()

    # Combine available távmért data frames
    combined_df = pd.concat(list(sources_dict.values()), ignore_index=True)

    # 2. FETCH DATABASE DATA & SAVE NOMINÁLT DATA
    nom_df = fetch_nominated_volumes(TARGET_GAS_DAY)
    nom_df.to_csv(OUTPUT_NOMINALT, index=False, encoding="utf-8-sig")
    print(f"[SUCCESS] Saved Nominált data ({len(nom_df)} rows) to: {OUTPUT_NOMINALT}")

    pod_map_df = None  # Initialize to keep scope clean
    multiplier_issue_msg = ""

    if combined_df.empty:
        oras_pod_df = pd.DataFrame(columns=["id_pod", "pod", "total_kwh_fogyasztas"])
    else:
        pod_map_df = fetch_pod_mapping()
        multipliers_df, multipliers_months_back = fetch_multipliers()

        # Determine a safe fallback multiplier when conversion values are missing.
        # Prefer the mean of available multipliers, otherwise fall back to a sensible default (10.7).
        if multipliers_df is None or multipliers_df.empty:
            print("[MULTIPLIER WARN] No conversion multipliers found; using fallback 10.7 kWh/m3")
            default_szorzo = 10.7
        else:
            mean_vals = multipliers_df["szorzo"].dropna()
            default_szorzo = float(mean_vals.mean()) if not mean_vals.empty else 10.7

        # Step 1: Join files POD -> id_pod via POD_list_m0
        merged_df = pd.merge(combined_df, pod_map_df, on="POD", how="inner")

        # Step 2: Keep ONLY PODs where id_pod is present in nom_pod_last
        active_nom_ids = set(nom_df["id_pod"].dropna().unique())
        filtered_df = merged_df[merged_df["id_pod"].isin(active_nom_ids)].copy()

        # Step 3: Join multiplier (szorzo) where POD matches
        filtered_df = pd.merge(filtered_df, multipliers_df, on="POD", how="left")
        # Fill missing multipliers with the computed safe fallback (avoid multiplying by 0)
        filtered_df["szorzo"] = filtered_df["szorzo"].fillna(default_szorzo)

        # Decide whether to add a multiplier diagnostic to final summary
        try:
            mb = int(multipliers_months_back)
        except Exception:
            mb = 0

        if mb == 0:
            multiplier_issue_msg = f"No multipliers found; used fallback {default_szorzo:.2f} kWh/m3"
        elif mb != 1:
            multiplier_issue_msg = f"Using multipliers from {mb} month(s) ago"

        # Calculate kWh consumption
        filtered_df["fogyasztas_kwh"] = filtered_df["fogyasztas_m3"] * filtered_df["szorzo"]

        # Step 4: Aggregate to single row per POD
        oras_pod_df = (
            filtered_df.groupby(["id_pod", "POD"], as_index=False)["fogyasztas_kwh"]
            .sum()
            .rename(columns={"POD": "pod", "fogyasztas_kwh": "total_kwh_fogyasztas"})
        )

        # SPECIAL OVERRIDE FOR EXCEPTION POD
        # SPECIAL OVERRIDE: apply nominal value only for the specific exceptional POD
        special_pod_mask = (
            (oras_pod_df["pod"] == "39N050777442000Z")
            | (oras_pod_df["id_pod"] == "28210101")
        )

        if special_pod_mask.any():
            for idx in oras_pod_df[special_pod_mask].index:
                target_id = oras_pod_df.loc[idx, "id_pod"]
                nom_match = nom_df[nom_df["id_pod"] == target_id]
                if not nom_match.empty:
                    nom_vol = nom_match["nomvol_kwh"].values[0]
                    oras_pod_df.loc[idx, "total_kwh_fogyasztas"] = nom_vol
                    print(
                        f"[SPECIAL OVERRIDE] POD {oras_pod_df.loc[idx, 'pod']} "
                        f"(id: {target_id}) forced to nominalt value: {nom_vol} kWh"
                    )

    oras_pod_df.to_csv(OUTPUT_ORAS_POD, index=False, encoding="utf-8-sig")
    print(f"[SUCCESS] Saved Távmért data ({len(oras_pod_df)} rows) to: {OUTPUT_ORAS_POD}")

    # 3. BUILD CONSOLIDATED PORTFOLIO
    portfolio_df = pd.merge(
        nom_df,
        oras_pod_df[["id_pod", "total_kwh_fogyasztas"]],
        on="id_pod",
        how="left",
    )

    portfolio_df["fogyasztas_kwh"] = (
        portfolio_df["total_kwh_fogyasztas"]
        .combine_first(portfolio_df["nomvol_kwh"])
        .fillna(0)
    )

    portfolio_df["source"] = portfolio_df["total_kwh_fogyasztas"].apply(
        lambda x: "tavmert" if pd.notna(x) else "profilos_nominalt"
    )

    output_portfolio_df = portfolio_df[[
        "id_pod",
        "gasday",
        "fogyasztas_kwh",
        "source",
        "nomvol_kwh",
    ]]

    output_portfolio_df.to_csv(OUTPUT_PORTFOLIO, index=False, encoding="utf-8-sig")
    print(f"[SUCCESS] Consolidated Portfolio ({len(output_portfolio_df)} rows) saved to: {OUTPUT_PORTFOLIO}")

    # --- NEW: TOP 10 TÚLNOMINÁLÁS & ALULNOMINÁLÁS SUMMARY TABLE ---
    if pod_map_df is None:
        pod_map_df = fetch_pod_mapping()

    summary_elteres_df = generate_top_bottom_elteres_table(portfolio_df, pod_map_df, FOGAZ_PATH, ED_PATH)

    if not summary_elteres_df.empty:
        output_top_bottom_path = BASE_DIR / "alul_felul_nominalas.csv"
        summary_elteres_df.to_csv(output_top_bottom_path, index=False, encoding="utf-8-sig")

        print("\n" + "=" * 80)
        print(" TOP 10 TÚLNOMINÁLÁS & ALULNOMINÁLÁS SUMMARY")
        print("=" * 80)
        print(summary_elteres_df.to_string(index=False))
        print("=" * 80 + "\n")

        # Export image & upload ONLY the image to Drive
        image_filename = SUMMARY_IMAGE_NAME
        saved_image_path = export_styled_summary_image(summary_elteres_df, image_filename)
        summary_image_url = None
        if saved_image_path:
            summary_image_url = helper_upload_to_drive(saved_image_path)

        # Print or format into your final chat message
        if summary_image_url:
            print(f"\n[CHAT MESSAGE LINK] Alul/Felul Nominalas Summary Image:\n{summary_image_url}")
        else:
            print("\n[INFO] Summary image created locally, but Drive upload was skipped or failed.")

    # 4. CALCULATION & EXCEL EQUIVALENT SUMMARY
    total_tavmert = float(oras_pod_df["total_kwh_fogyasztas"].sum()) if not oras_pod_df.empty else 0.0
    total_nominalt = float(nom_df["nomvol_kwh"].sum()) if not nom_df.empty else 0.0

    if not oras_pod_df.empty and not nom_df.empty:
        tavmert_pod_ids = set(oras_pod_df["id_pod"].dropna().unique())
        nom_vol_if_inc = float(nom_df[nom_df["id_pod"].isin(tavmert_pod_ids)]["nomvol_kwh"].sum())
    else:
        nom_vol_if_inc = 0.0

    nom_origin = fetch_nominated_origin_vol(TARGET_GAS_DAY)
    profilos_vol = max(nom_origin - total_nominalt, 0.0)

    # If NO files were found at all, portfolio = nominalt + profilos
    if combined_df.empty:
        portfolio = total_nominalt + profilos_vol
    else:
        portfolio = total_tavmert + (total_nominalt - nom_vol_if_inc) + profilos_vol

    a_long_data = fetch_a_long_data(TARGET_GAS_DAY)
    sum_long = a_long_data["SUM_LONG"]

    lpfs_raw = a_long_data["lpfs_carry"]
    store_raw = a_long_data["store_MFGT"]

    lpfs = abs(lpfs_raw) if lpfs_raw < 0 else 0.0
    betar = abs(store_raw) if store_raw < 0 else 0.0

    net_sum = sum_long - portfolio - lpfs - betar

    pf_k = round(portfolio / 1000)
    balance_k = abs(round(net_sum / 1000))
    position_type = " SHORT" if net_sum < 0 else " LONG"

    summary_string = f"Reggeli becslés: PF {pf_k}K, balance: {balance_k}K{position_type}"
    if missing_sources:
        missing_str = ", ".join(missing_sources)
        summary_string += f"\nHiányzó adatok: {missing_str}"
    # Append multiplier diagnostics only when there was an issue (not the preferred 1-month lookup)
    if multiplier_issue_msg:
        summary_string += f"\n{multiplier_issue_msg}"
    if summary_image_url:
        summary_string += f"\nAlul/Felül Nominálás: {summary_image_url}"

    print("\n" + "=" * 65)
    print(" POSITION BREAKDOWN (kWh)")
    print("=" * 65)
    print(f"  Ossz export (J616):                          {total_tavmert:>15,.2f} kWh")
    print(f"  Ossz nom (H616):                             {total_nominalt:>15,.2f} kWh")
    print(f"  Nom vol if inc (I616):                       {nom_vol_if_inc:>15,.2f} kWh")
    print(f"  Nom origin (H618):                           {nom_origin:>15,.2f} kWh")
    print(f"  Profilos (H617):                             {profilos_vol:>15,.2f} kWh")
    print("-" * 65)
    print(f"  PORTFOLIO = J616+(H616-I616)+H617:           {portfolio:>15,.2f} kWh")
    print("=" * 65)
    print(f"  Osszes forras (sum_long):                    {sum_long:>15,.2f} kWh")
    print(f"  LPFS:                                        {lpfs:>15,.2f} kWh")
    print(f"  Betár:                                       {betar:>15,.2f} kWh")
    print("-" * 65)
    print(f"  SUM (osszes forras - portfolio-lpfs-betar): {net_sum:>15,.2f} kWh")
    print("=" * 65)
    print(f"  FINAL STRING: {summary_string}")
    print("=" * 65 + "\n")

    # 5. GOOGLE CHAT FINAL SUMMARY DELIVERY
    if SEND_CHAT:
        try:
            wattler_chat.send_chat(CHAT_ID, summary_string)
            print(f"[CHAT] Final summary delivered to {CHAT_ID} space.")
        except Exception as e:
            print(f"[CHAT ERROR] Failed to send final chat message: {e}")
    else:
        print("[CHAT DISABLED] Final summary not sent (SEND_CHAT=False)")

    return summary_string


if __name__ == "__main__":
    main()