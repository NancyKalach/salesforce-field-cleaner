import re
from typing import List, Dict, Tuple
import pandas as pd
from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceGeneralError, SalesforceMalformedRequest

# === Tunables / sensible defaults ===
DEFAULT_MAX_RECORDS = 5000          # analyze most-recent N records
CHUNK_SIZE = 150                    # fields per SELECT chunk

# System/critical fields that should NEVER be auto-flagged as safe to delete,
# regardless of usage or dependencies.
PROTECTED_FIELDS = {
    "Id", "IsDeleted", "CreatedById", "CreatedDate",
    "LastModifiedById", "LastModifiedDate", "SystemModstamp",
    "OwnerId", "Name", "RecordTypeId", "LastReferencedDate", "LastViewedDate"
}

def _rest_next_rel(sf: Salesforce, next_url: str) -> str:
    """Convert absolute '/services/data/vXX.X/...' to a relative path for sf.restful."""
    prefix = f"/services/data/v{sf.sf_version}/"
    return next_url[len(prefix):] if next_url.startswith(prefix) else next_url.lstrip("/")

def _tooling_query(sf: Salesforce, soql: str) -> List[Dict]:
    """
    Run a Tooling API query using simple_salesforce's base_url with safe pagination.
    """
    # First page
    res = sf.restful("tooling/query", params={"q": soql})
    records = list(res.get("records", []))

    # Paginate
    while not res.get("done", True):
        next_url = res.get("nextRecordsUrl")
        if not next_url:
            break
        res = sf.restful(_rest_next_rel(sf, next_url))
        records.extend(res.get("records", []))

    return records

def _chunk(lst: List[str], size: int) -> List[List[str]]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]

def _fetch_field_describe(sf: Salesforce, sobject: str) -> Tuple[List[str], Dict[str, str]]:
    """
    Return fields that are safe to SELECT to avoid SOQL errors on compound/blob/unqueryable.
    """
    fields = sf.__getattr__(sobject).describe()["fields"]
    names: List[str] = []
    types: Dict[str, str] = {}

    for f in fields:
        t = f.get("type")
        # Skip unqueryable, deprecated/hidden, or problematic bulk-select types
        if not f.get("queryable", True):
            continue
        if f.get("deprecatedAndHidden", False):
            continue
        if t in {"address", "location", "base64"}:
            continue

        names.append(f["name"])
        types[f["name"]] = t

    return names, types

def _fetch_records_in_chunks(
    sf: Salesforce,
    sobject: str,
    field_names: List[str],
    max_records: int = DEFAULT_MAX_RECORDS,
    chunk_size: int = CHUNK_SIZE
) -> pd.DataFrame:
    """
    Fetch the most-recent max_records rows for the object by adding
    ORDER BY CreatedDate DESC, Id DESC LIMIT max_records to every field chunk query.

    This avoids huge WHERE Id IN (...) lists (no 414) and stays on GET /query,
    while ensuring each chunk returns the same row set (consistent merges),
    assuming no concurrent inserts/updates during the run.
    """
    # Always ensure Id is present (for merging)
    if "Id" not in field_names:
        field_names = ["Id"] + field_names

    # Split fields into chunks (exclude Id from chunking)
    field_chunks = _chunk([f for f in field_names if f != "Id"], chunk_size)
    frames: List[pd.DataFrame] = []

    for i, ch in enumerate(field_chunks, start=1):
        select_list = ", ".join(["Id"] + ch)
        soql = (
            f"SELECT {select_list} "
            f"FROM {sobject} "
            f"ORDER BY CreatedDate DESC, Id DESC "
            f"LIMIT {int(max_records)}"
        )
        print(f"  • Query chunk {i}/{len(field_chunks)} ({1 + len(ch)} fields) …")
        res = sf.query_all(soql)["records"]
        for r in res:
            r.pop("attributes", None)
        df = pd.DataFrame(res)
        frames.append(df)

    if not frames:
        return pd.DataFrame(columns=field_names)

    # Merge all field-chunk frames on Id
    base = frames[0]
    for f in frames[1:]:
        base = base.merge(f, on="Id", how="inner", suffixes=("", "_dup"))
        # Clean any accidental dup columns
        dup_cols = [c for c in base.columns if c.endswith("_dup")]
        if dup_cols:
            base.drop(columns=dup_cols, inplace=True)

    # Enforce column order/presence
    for col in field_names:
        if col not in base.columns:
            base[col] = pd.NA
    return base[field_names]

def _scan_dependencies(sf: Salesforce, sobject: str, field_names: List[str], fast_prefilter: bool = False) -> pd.DataFrame:
    """
    Scan Apex classes, triggers, and validation rules to see where fields are referenced.
    Only the fields passed in `field_names` are checked (pass 0%-populated fields to speed it up).

    NOTE: We DO NOT filter on Body (unsupported in many orgs). If fast_prefilter=True is requested,
    we attempt it and safely fall back to unfiltered if the org disallows it.
    """
    cols = ["Field API Name", "Referenced In", "Name", "Active"]
    if not field_names:
        return pd.DataFrame(columns=cols)

    print("🔎 Scanning dependencies via Tooling API (Apex & Validation Rules)…")
    print(f"   • Fields to check: {len(field_names)}")

    # Base SOQL (no Body filter)
    class_soql = "SELECT Id, Name, Body FROM ApexClass"
    trig_soql  = "SELECT Id, Name, Body FROM ApexTrigger"

    apex_classes = None
    triggers = None

    if fast_prefilter:
        try:
            apex_classes = _tooling_query(sf, f"{class_soql} WHERE Body LIKE '%{sobject}%'")
            triggers     = _tooling_query(sf, f"{trig_soql} WHERE Body LIKE '%{sobject}%'")
        except (SalesforceGeneralError, SalesforceMalformedRequest):
            print("   • Prefilter on Body not supported in this org. Falling back to full scan.")
            apex_classes = None
            triggers = None

    if apex_classes is None:
        apex_classes = _tooling_query(sf, class_soql)
    if triggers is None:
        triggers = _tooling_query(sf, trig_soql)

    # Validation Rules list (we'll fetch formula via Metadata per rule)
    vr_list = _tooling_query(
        sf,
        f"SELECT Id, ValidationName, Active, EntityDefinition.QualifiedApiName "
        f"FROM ValidationRule WHERE EntityDefinition.QualifiedApiName = '{sobject}'"
    )
    print(f"   • Apex classes: {len(apex_classes)} | Triggers: {len(triggers)} | Validation Rules: {len(vr_list)}")

    # Compile regex patterns once per field
    patterns = {
        fld: [
            re.compile(rf"\b{re.escape(fld)}\b", re.IGNORECASE),
            re.compile(rf"\b{re.escape(sobject)}\s*\.\s*{re.escape(fld)}\b", re.IGNORECASE),
        ]
        for fld in field_names
    }

    def _find_hits(text: str, fld: str) -> bool:
        t = text or ""
        for p in patterns.get(fld, []):
            if p.search(t):
                return True
        return False

    rows: List[Dict] = []

    # Scan Apex classes
    for i, apx in enumerate(apex_classes, 1):
        body = apx.get("Body") or ""
        for fld in field_names:
            if _find_hits(body, fld):
                rows.append({"Field API Name": fld, "Referenced In": "ApexClass",
                             "Name": apx.get("Name"), "Active": ""})
        if i % 50 == 0:
            print(f"     • Scanned {i}/{len(apex_classes)} classes…")

    # Scan triggers
    for j, trg in enumerate(triggers, 1):
        body = trg.get("Body") or ""
        for fld in field_names:
            if _find_hits(body, fld):
                rows.append({"Field API Name": fld, "Referenced In": "ApexTrigger",
                             "Name": trg.get("Name"), "Active": ""})
        if j % 25 == 0:
            print(f"     • Scanned {j}/{len(triggers)} triggers…")

    # Scan validation rules (need Metadata to read formula)
    for k, vr in enumerate(vr_list, 1):
        vr_id     = vr.get("Id")
        vr_name   = vr.get("ValidationName")
        vr_active = vr.get("Active")
        try:
            vr_full = sf.restful(f"tooling/sobjects/ValidationRule/{vr_id}")
            meta    = (vr_full or {}).get("Metadata", {})
            formula = meta.get("errorConditionFormula", "") or ""
        except Exception as e:
            print(f"     • Skipped VR {vr_name} ({vr_id}) due to error: {e}")
            formula = ""

        for fld in field_names:
            if _find_hits(formula, fld):
                rows.append({"Field API Name": fld, "Referenced In": "ValidationRule",
                             "Name": vr_name, "Active": vr_active})

        if k % 25 == 0:
            print(f"     • Scanned {k}/{len(vr_list)} validation rules…")

    return pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)

def _build_usage_frame(df: pd.DataFrame, field_types: Dict[str, str]) -> pd.DataFrame:
    if df.empty:
        print("ℹ️  No records returned. Usage will show 0% populated for all fields.")
        return pd.DataFrame(columns=[
            "Field API Name", "Field Type", "Null Count", "Populated Count", "Percent Populated"
        ])

    total_records = len(df)
    null_counts = df.isna().sum()

    # Exclude Id from usage stats
    if "Id" in null_counts.index:
        null_counts = null_counts[null_counts.index != "Id"]

    usage = pd.DataFrame({
        "Field API Name": null_counts.index,
        "Field Type": [field_types.get(name, "") for name in null_counts.index],
        "Null Count": null_counts.values,
        "Populated Count": total_records - null_counts.values,
        "Percent Populated": ((total_records - null_counts.values) / max(total_records, 1) * 100).round(2)
    }).sort_values(by="Percent Populated")

    return usage

def _mark_safe_to_delete(usage: pd.DataFrame, deps: pd.DataFrame) -> pd.DataFrame:
    """
    Strict rule:
      - Not in PROTECTED_FIELDS
      - Dependency Count == 0 (only meaningful if scanned)
      - Percent Populated == 0
    Also exposes:
      - Candidate (0% pop)
      - Scanned for Dependencies? (True only for candidates)
    """
    # Ensure candidate flag exists for clarity in the output
    if "Candidate (0% pop)" not in usage.columns:
        usage = usage.copy()
        usage["Candidate (0% pop)"] = (usage["Percent Populated"] == 0)

    # Merge dependency counts (only for scanned candidates)
    if deps is not None and isinstance(deps, pd.DataFrame) and not deps.empty:
        dep_counts = deps.groupby("Field API Name").size().rename("Dependency Count")
    else:
        dep_counts = pd.Series(dtype=int, name="Dependency Count")

    out = usage.merge(dep_counts, on="Field API Name", how="left")

    # Mark which rows were actually scanned
    out["Scanned for Dependencies?"] = out["Candidate (0% pop)"]

    # Fill dependency count ONLY for scanned rows; leave others as <NA>
    out["Dependency Count"] = out["Dependency Count"].astype("Float64")  # allow NA
    scanned_mask = out["Scanned for Dependencies?"] == True
    out.loc[scanned_mask, "Dependency Count"] = out.loc[scanned_mask, "Dependency Count"].fillna(0)
    # Cast to nullable integer for nicer display (NA stays NA)
    out["Dependency Count"] = out["Dependency Count"].astype("Int64")

    # Apply strict safe-to-delete rule
    def _safe(row):
        name = row["Field API Name"]
        if name in PROTECTED_FIELDS:
            return False
        if row["Percent Populated"] != 0:
            return False
        # Only trust Dependency Count when scanned; if NA, treat as not safe
        dep = row["Dependency Count"]
        if pd.isna(dep):
            return False
        return int(dep) == 0

    out["Safe to Delete?"] = out.apply(_safe, axis=1)
    return out

def generate_usage_report(
    username: str,
    password: str,
    # token: str,
    domain: str,
    sobject: str,
    file_type: str,
    run_dependency_scan: bool = True,
    max_records: int = DEFAULT_MAX_RECORDS
):
    try:
        print(f"\n🔗 Connecting to {domain}.salesforce.com …")
        # If your org requires a token, pass it here; using empty '' assumes IP whitelisting/session policy allows it.
        sf = Salesforce(username=username, password=password, security_token='', domain=domain)

        print(f"🧭 Describing {sobject} fields …")
        field_names, field_types = _fetch_field_describe(sf, sobject)
        if not field_names:
            print("No fields found.")
            return

        print(f"🧮 Fetching records (most recent {max_records}) with chunked SOQL …")
        df = _fetch_records_in_chunks(sf, sobject, field_names, max_records=max_records)

        print("📊 Computing usage metrics …")
        usage = _build_usage_frame(df, field_types)
        usage["Candidate (0% pop)"] = (usage["Percent Populated"] == 0)  # transparency

        cols = ["Field API Name", "Referenced In", "Name", "Active"]

        # Only check 0%-populated fields to speed up dependency scanning
        if run_dependency_scan:
            zero_pop_fields = usage.loc[usage["Candidate (0% pop)"], "Field API Name"].tolist()
            candidate_set = set(zero_pop_fields)
            if zero_pop_fields:
                deps = _scan_dependencies(sf, sobject, zero_pop_fields, fast_prefilter=False)
                # Keep deps strictly to candidates (defensive)
                if isinstance(deps, pd.DataFrame) and not deps.empty:
                    deps = deps.loc[deps["Field API Name"].isin(candidate_set)].copy()
                else:
                    deps = pd.DataFrame(columns=cols)
            else:
                deps = pd.DataFrame(columns=cols)
        else:
            deps = pd.DataFrame(columns=cols)

        # Ensure deps is a DataFrame
        if not isinstance(deps, pd.DataFrame):
            deps = pd.DataFrame(columns=cols)

        print("🧹 Applying strict safe-to-delete rule …")
        usage_with_flags = _mark_safe_to_delete(usage, deps)

        # Summary sheet
        total_fields   = len(usage_with_flags)
        with_deps      = int((usage_with_flags["Dependency Count"].fillna(0) > 0).sum()) if not usage_with_flags.empty else 0
        zero_pop_count = int((usage_with_flags["Percent Populated"] == 0).sum()) if not usage_with_flags.empty else 0
        safe_to_delete = int(usage_with_flags["Safe to Delete?"].sum()) if not usage_with_flags.empty else 0

        summary = pd.DataFrame([
            {"Metric": "Total Records Scanned", "Value": len(df)},
            {"Metric": "Total Fields", "Value": total_fields},
            {"Metric": "Fields with 0% populated", "Value": zero_pop_count},
            {"Metric": "Fields with Dependencies", "Value": with_deps},
            {"Metric": "Safe to Delete (strict rule)", "Value": safe_to_delete},
        ])

        filename = f"{sobject.lower()}_field_deprecation_assessment.{ 'xlsx' if file_type == 'excel' else 'csv' }"
        print(f"💾 Saving to {filename} …")

        if file_type == "excel":
            with pd.ExcelWriter(filename, engine="openpyxl") as writer:
                summary.to_excel(writer, sheet_name="Summary", index=False)
                # Sort: safe first, fewest deps, lowest usage (NA counts will naturally float last)
                usage_with_flags.sort_values(
                    by=["Safe to Delete?", "Dependency Count", "Percent Populated"],
                    ascending=[False, True, True]
                ).to_excel(writer, sheet_name="FieldUsage", index=False)
                deps.sort_values(["Field API Name", "Referenced In", "Name"]).to_excel(writer, sheet_name="Dependencies", index=False)
        else:
            usage_with_flags.to_csv(filename, index=False)
            deps.to_csv(f"{sobject.lower()}_dependencies.csv", index=False)
            summary.to_csv(f"{sobject.lower()}_summary.csv", index=False)

        print("✅ Report generation complete.")

    except (SalesforceGeneralError, SalesforceMalformedRequest) as e:
        print("❌ Salesforce error:", getattr(e, "content", e))
    except Exception as e:
        print("❌ Unexpected error:", e)
