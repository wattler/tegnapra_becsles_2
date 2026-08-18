from datetime import datetime, date, timedelta
from pathlib import Path
import pandas as pd
from wattler_tools import wdb

# --- CONFIGURATION & PATHS ---
BASE_DIR = Path(__file__).parent
FOGAZ_PATH = BASE_DIR / "fogaz.csv"
ED_PATH = BASE_DIR / "ed.csv"
TIGAZ_PATH = BASE_DIR / "tigaz.xlsx"

OUTPUT_ORAS_POD = BASE_DIR / "oras_pod.csv"
OUTPUT_NOMINALT = BASE_DIR / "nominalt.csv"
OUTPUT_PORTFOLIO = BASE_DIR / "portfolio.csv"

# Target gas day is always D-1 (yesterday)
TARGET_GAS_DAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")


# --- HELPER FUNCTIONS ---
def parse_gasday_from_interval_start(dt_series: pd.Series) -> pd.Series:
    """
    For FŐGÁZ / ÉD ('Gáznap -tól'): Timestamps mark the START of the hour.
    Example: '2026.08.17 06:00' to '05:00' next day = Gas Day '2026-08-17'
    Hours 00:00 to 05:00 belong to the PREVIOUS calendar day's gas day.
    """
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
    """
    For TIGÁZ ('Időbélyeg'): Timestamps mark the END of the hour.
    Example: '2026-08-17 07:00' (06:00-07:00) through '2026-08-18 06:00' (05:00-06:00) = Gas Day '2026-08-17'
    Hours 00:00 to 06:00 belong to the PREVIOUS calendar day's gas day.
    """
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


def load_and_filter_standard_csv(file_path: Path, source_name: str) -> pd.DataFrame:
    """Loads Főgáz/ÉD CSVs, parses standard columns, filters by target gas day, and aggregates per POD."""
    if not file_path.exists():
        print(f"[{source_name.upper()}] File not found at {file_path}, skipping.")
        return pd.DataFrame(columns=["POD", "fogyasztas_m3"])

    df = load_csv_with_encoding(file_path)
    df.columns = df.columns.astype(str).str.strip()

    target_col = [c for c in df.columns if "Gáznap" in c and "tól" in c][0]
    df["calculated_gasday"] = parse_gasday_from_interval_start(df[target_col])

    valid_df = df[df["calculated_gasday"] == TARGET_GAS_DAY].copy()

    if valid_df.empty:
        print(f"[{source_name.upper()}] 0 rows found for Gas Day: {TARGET_GAS_DAY}")
        return pd.DataFrame(columns=["POD", "fogyasztas_m3"])

    korrigalt_col = [c for c in valid_df.columns if "Korrigált fogyasztás" in c][0]
    uzemi_col = [c for c in valid_df.columns if "Üzemi fogyasztás" in c][0]
    pod_col = [c for c in valid_df.columns if "POD" in c][0]

    korr_vals = clean_numeric_column(valid_df[korrigalt_col])
    uzemi_vals = clean_numeric_column(valid_df[uzemi_col])

    fogyasztas = korr_vals.combine_first(uzemi_vals).fillna(0)
    pod_series = valid_df[pod_col].astype(str).str.strip()

    temp_df = pd.DataFrame({
        "POD": pod_series,
        "fogyasztas_m3": fogyasztas,
    })

    # Group by POD to sum hourly rows into 1 daily value per POD
    out_df = temp_df.groupby("POD", as_index=False)["fogyasztas_m3"].sum()

    print(f"[{source_name.upper()}] Loaded {len(out_df)} unique PODs for Gas Day: {TARGET_GAS_DAY}")
    return out_df


def load_and_filter_tigaz_xlsx(file_path: Path) -> pd.DataFrame:
    """Loads Tigáz XLSX using exact column headers, implements Excel IF logic, and aggregates per POD."""
    if not file_path.exists():
        print(f"[TIGAZ] Excel file not found at {file_path}, skipping.")
        return pd.DataFrame(columns=["POD", "fogyasztas_m3"])

    df = pd.read_excel(file_path, engine="openpyxl")
    df.columns = df.columns.astype(str).str.strip()

    df["calculated_gasday"] = parse_gasday_from_interval_end(df["Időbélyeg"])
    valid_df = df[df["calculated_gasday"] == TARGET_GAS_DAY].copy()

    if valid_df.empty:
        print(f"[TIGAZ] 0 rows found for Gas Day: {TARGET_GAS_DAY}")
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

    # Group by POD to sum hourly rows into 1 daily value per POD
    out_df = temp_df.groupby("POD", as_index=False)["fogyasztas_m3"].sum()

    print(f"[TIGAZ] Loaded {len(out_df)} unique PODs for Gas Day: {TARGET_GAS_DAY}")
    return out_df


def fetch_pod_mapping() -> pd.DataFrame:
    """Fetches POD_azonosito to id_POD mapping without filtering by isTavmert."""
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
    """Fetches GCV multipliers (szorzo)."""
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
        df = pd.read_sql(sql_multipliers, con=conn)
    return df

def fetch_nominated_volumes(target_gas_day: str) -> pd.DataFrame:
    """Fetches nominated POD volumes using the nom_pod_last SQL conditions."""
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
    """Fetches store_MFGT, lpfs_carry, and SUM_LONG from W7_gas_alloc.a_long for target_gas_day."""
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
    """
    Fetches the original total nominated exit volume (vol_kwh_pf) for target_gas_day.
    Replicates: =VLOOKUP(IF(ISBLANK('Nom db'!B4), TODAY() - 1, 'Nom db'!B4), 'Nom db'!I:J, 2, FALSE)
    """
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


# --- MAIN EXECUTION ---
def main():
    print(f"Processing data for Gas Day: {TARGET_GAS_DAY}\n")

    # ---------------------------------------------------------
    # 1. PROCESS TÁVMÉRT (HOURLY METERED) FILES
    # ---------------------------------------------------------
    df_fogaz = load_and_filter_standard_csv(FOGAZ_PATH, "fogaz")
    df_ed = load_and_filter_standard_csv(ED_PATH, "ed")
    df_tigaz = load_and_filter_tigaz_xlsx(TIGAZ_PATH)

    combined_df = pd.concat([df_fogaz, df_ed, df_tigaz], ignore_index=True)
    raw_file_pods_count = combined_df["POD"].nunique()
    print(f"Total unique PODs in raw files: {raw_file_pods_count}")

    # ---------------------------------------------------------
    # 2. FETCH DATABASE DATA & SAVE NOMINÁLT DATA
    # ---------------------------------------------------------
    nom_df = fetch_nominated_volumes(TARGET_GAS_DAY)
    nom_df.to_csv(OUTPUT_NOMINALT, index=False, encoding="utf-8-sig")
    print(f"[SUCCESS] Saved Nominált data ({len(nom_df)} rows) to: {OUTPUT_NOMINALT}")

    if combined_df.empty:
        print("[WARNING] No távmért data loaded from input files!")
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

        # ---------------------------------------------------------
        # EXCEL OVERRIDE LOGIC: IF(A605=28210101, H605, vv)
        # For POD 28210101, replace exported consumption with nominated volume (nomvol_kwh)
        # ---------------------------------------------------------
        pod_28210101_mask = oras_pod_df["pod"] == "28210101"
        if pod_28210101_mask.any():
            pod_28210101_id = oras_pod_df.loc[pod_28210101_mask, "id_pod"].values[0]
            nom_match = nom_df[nom_df["id_pod"] == pod_28210101_id]
            if not nom_match.empty:
                nom_vol = nom_match["nomvol_kwh"].values[0]
                oras_pod_df.loc[pod_28210101_mask, "total_kwh_fogyasztas"] = nom_vol
                print(f"[SPECIAL LOGIC] Replaced consumption for POD 28210101 with nominated volume: {nom_vol} kWh")

    oras_pod_df.to_csv(OUTPUT_ORAS_POD, index=False, encoding="utf-8-sig")
    print(f"[SUCCESS] Saved Távmért data ({len(oras_pod_df)} rows) to: {OUTPUT_ORAS_POD}")

    # ---------------------------------------------------------
    # 3. BUILD CONSOLIDATED PORTFOLIO
    # ---------------------------------------------------------
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

    # ---------------------------------------------------------
    # 4. CALCULATION & EXCEL EQUIVALENT SUMMARY
    # ---------------------------------------------------------
    total_tavmert = float(oras_pod_df["total_kwh_fogyasztas"].sum()) if not oras_pod_df.empty else 0.0
    total_nominalt = float(nom_df["nomvol_kwh"].sum()) if not nom_df.empty else 0.0

    if not oras_pod_df.empty and not nom_df.empty:
        tavmert_pod_ids = set(oras_pod_df["id_pod"].dropna().unique())
        nom_vol_if_inc = float(nom_df[nom_df["id_pod"].isin(tavmert_pod_ids)]["nomvol_kwh"].sum())
    else:
        nom_vol_if_inc = 0.0

    nom_origin = fetch_nominated_origin_vol(TARGET_GAS_DAY)
    profilos_vol = max(nom_origin - total_nominalt, 0.0)

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
    print(f"  LPFS:                                         {lpfs:>15,.2f} kWh")
    print(f"  Betár:                                        {betar:>15,.2f} kWh")
    print("-" * 65)
    print(f"  SUM (osszes forras - portfolio-lpfs-betar): {net_sum:>15,.2f} kWh")
    print("=" * 65)
    print(f"  FINAL STRING: {summary_string}")
    print("=" * 65 + "\n")

    return summary_string

if __name__ == "__main__":
    main()