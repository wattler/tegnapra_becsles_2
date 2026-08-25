from datetime import datetime, date, timedelta
from pathlib import Path
import time
import pandas as pd
import numpy as np
from wattler_tools import wdb, wattler_drive, wattler_chat

# --- CONFIGURATION & PATHS ---
BASE_DIR = Path(__file__).parent
FOGAZ_PATH = BASE_DIR / "fogaz.csv"
ED_PATH = BASE_DIR / "ed.csv"
TIGAZ_PATH = BASE_DIR / "tigaz.xlsx"

# Google Drive Shared Folder ID containing the raw files
SHARED_DRIVE_FOLDER_ID = "1XHfnTEHt3GKgS8S-R2f0wcSpb8pP__yG"

CHAT_ID = "TESZT"

OUTPUT_ORAS_POD = BASE_DIR / "oras_pod.csv"
OUTPUT_NOMINALT = BASE_DIR / "nominalt.csv"
OUTPUT_PORTFOLIO = BASE_DIR / "portfolio.csv"

# Target gas day is always D-1 (yesterday)
TARGET_GAS_DAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

# Retry settings: 10 minutes wait between checks
RETRY_DELAY_SECONDS = 600  # 10 minutes
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
        valid_nem_df = valid_df[is_nem]
        
        nem_means = valid_nem_df.groupby(gyari_col)["raw_m3"].mean()
        mapped_means = valid_df[gyari_col].map(nem_means)

        valid_df["fogyasztas_m3"] = np.where(is_nem, valid_df["raw_m3"], mapped_means)

        fully_imputed_pods = valid_df[valid_df["fogyasztas_m3"].isna()]["POD"].unique()
        if len(fully_imputed_pods) > 0:
            print(f"[{source_name.upper()}] Dropped {len(fully_imputed_pods)} POD(s) with 100% 'igen' rows: {list(fully_imputed_pods)}")
            valid_df = valid_df[~valid_df["POD"].isin(fully_imputed_pods)].copy()
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
        CAST(POD_azonosito AS CHAR) AS POD 
    FROM 
        W2_sites_pods.POD_list_m0
    """
    with engine.connect() as conn:
        return pd.read_sql(sql_pod, con=conn)


def fetch_multipliers() -> pd.DataFrame:
    engine = wdb.get_sqla_engine()
    sql_multipliers = """
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
        ca.gasmonth = DATE_FORMAT(LAST_DAY(CURDATE() - INTERVAL 1 MONTH), '%Y-%m-01')
    ORDER BY POD
    """
    with engine.connect() as conn:
        return pd.read_sql(sql_multipliers, con=conn)


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

        # If we found at least one file or reached maximum retries, proceed
        if found_sources or attempt == MAX_RETRIES:
            return sources_dict, found_sources, missing_sources

        # No files found on this attempt; send message, wait x mins, then retry
        retry_minutes = RETRY_DELAY_SECONDS // 60
        wait_msg = f"*DATA MISSING*: No valid Gas Day `{TARGET_GAS_DAY}` files found on Drive (FŐGÁZ, ÉD, TIGÁZ). Retrying in {retry_minutes} minutes..."
        print(f"[TIME RELAY] {wait_msg}")
        try:
            wattler_chat.send_chat(CHAT_ID, wait_msg)
        except Exception as e:
            print(f"[CHAT ERROR] Failed to send chat message: {e}")

        time.sleep(RETRY_DELAY_SECONDS)

    return sources_dict, found_sources, missing_sources


# --- MAIN EXECUTION ---
def main():
    print(f"Processing data for Gas Day: {TARGET_GAS_DAY}\n")

    # 1. RETRY / FETCH FILES FROM DRIVE
    sources_dict, found_sources, missing_sources = fetch_all_files_with_retry()

    # Inform Chat if some (or all) files were missing after checks
    if missing_sources and found_sources:
        status_msg = f"*PARTIAL DATA*: Found files for `{', '.join(found_sources)}`. Missing files: `{', '.join(missing_sources)}`. Using nominated values for missing sources."
        print(f"[INFO] {status_msg}")
        try:
            wattler_chat.send_chat(CHAT_ID, status_msg)
        except Exception as e:
            print(f"[CHAT ERROR] {e}")
    elif not found_sources:
        status_msg = f"*NO DATA FOUND*: Could not retrieve any raw files for Gas Day `{TARGET_GAS_DAY}`. Falling back strictly to nominated values."
        print(f"[INFO] {status_msg}")
        try:
            wattler_chat.send_chat(CHAT_ID, status_msg)
        except Exception as e:
            print(f"[CHAT ERROR] {e}")

    # Combine available távmért data frames
    combined_df = pd.concat(list(sources_dict.values()), ignore_index=True)

    # 2. FETCH DATABASE DATA & SAVE NOMINÁLT DATA
    nom_df = fetch_nominated_volumes(TARGET_GAS_DAY)
    nom_df.to_csv(OUTPUT_NOMINALT, index=False, encoding="utf-8-sig")
    print(f"[SUCCESS] Saved Nominált data ({len(nom_df)} rows) to: {OUTPUT_NOMINALT}")

    if combined_df.empty:
        oras_pod_df = pd.DataFrame(columns=["id_pod", "pod", "total_kwh_fogyasztas"])
    else:
        pod_map_df = fetch_pod_mapping()
        multipliers_df = fetch_multipliers()

        # Step 1: Join files POD -> id_pod via POD_list_m0
        merged_df = pd.merge(combined_df, pod_map_df, on="POD", how="inner")

        # Step 2: Keep ONLY PODs where id_pod is present in nom_pod_last
        active_nom_ids = set(nom_df["id_pod"].dropna().unique())
        filtered_df = merged_df[merged_df["id_pod"].isin(active_nom_ids)].copy()

        # Step 3: Join multiplier (szorzo) where POD matches
        filtered_df = pd.merge(filtered_df, multipliers_df, on="POD", how="left")
        filtered_df["szorzo"] = filtered_df["szorzo"].fillna(0.0)

        # Calculate kWh consumption
        filtered_df["fogyasztas_kwh"] = filtered_df["fogyasztas_m3"] * filtered_df["szorzo"]

        # Step 4: Aggregate to single row per POD
        oras_pod_df = (
            filtered_df.groupby(["id_pod", "POD"], as_index=False)["fogyasztas_kwh"]
            .sum()
            .rename(columns={"POD": "pod", "fogyasztas_kwh": "total_kwh_fogyasztas"})
        )

        # SPECIAL OVERRIDE FOR EXCEPTION POD
        special_pod_mask = (
            (oras_pod_df["pod"] == "39N050777442000Z")
            | (oras_pod_df["pod"] == "28210101")
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
    print(f"  Osszes forras (sum_long):                     {sum_long:>15,.2f} kWh")
    print(f"  LPFS:                                        {lpfs:>15,.2f} kWh")
    print(f"  Betár:                                       {betar:>15,.2f} kWh")
    print("-" * 65)
    print(f"  SUM (osszes forras - portfolio-lpfs-betar): {net_sum:>15,.2f} kWh")
    print("=" * 65)
    print(f"  FINAL STRING: {summary_string}")
    print("=" * 65 + "\n")

    # 5. GOOGLE CHAT FINAL SUMMARY DELIVERY
    try:
        wattler_chat.send_chat(CHAT_ID, summary_string)
        print(f"[CHAT] Final summary delivered to {CHAT_ID} space.")
    except Exception as e:
        print(f"[CHAT ERROR] Failed to send final chat message: {e}")

    return summary_string


if __name__ == "__main__":
    main()