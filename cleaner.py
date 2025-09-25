import math
import re
from typing import List, Dict, Tuple
import pandas as pd
from simple_salesforce import Salesforce
from simple_salesforce.exceptions import SalesforceGeneralError, SalesforceMalformedRequest

# Extended protected fields (minimal, safe additions)
PROTECTED_FIELDS = {
    "Id", "IsDeleted", "CreatedById", "CreatedDate",
    "LastModifiedById", "LastModifiedDate", "SystemModstamp",
    "OwnerId", "Name", "RecordTypeId", "LastReferencedDate", "LastViewedDate"
}

def _tooling_query(sf, soql: str) -> list[dict]:
    """
    Run a Tooling API query using simple_salesforce's base_url.
    IMPORTANT: pass a RELATIVE path ('tooling/query'), not '/services/data/...'.
    """
    # First page
    res = sf.restful("tooling/query", params={"q": soql})
    records = list(res.get("records", []))

    # Paginate
    while not res.get("done", True):
        next_url = res.get("nextRecordsUrl")
        if not next_url:
            break
        # nextRecordsUrl comes back like '/services/data/v59.0/tooling/query/01gXXXX-2000'
        # simple_salesforce can accept that absolute-style path as-is:
        res = sf.restful(next_url)
        records.extend(res.get("records", []))

    return records


def _chunk(lst: List[str], size: int) -> List[List[str]]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]

def _fetch_field_describe(sf: Salesforce, sobject: str) -> Tuple[List[str], Dict[str, str]]:
    """
    Keep only fields that are safe to SELECT to avoid SOQL errors on compound/blob/unqueryable.
    """
    fields = sf.__getattr__(sobject).describe()["fields"]
    names: List[str] = []
    types: Dict[str, str] = {}

    for f in fields:
        t = f.get("type")
        # Skip unqueryable or deprecated fields
        if not f.get("queryable", True):
            continue
        if f.get("deprecatedAndHidden", False):
            continue
        # Skip problematic types in bulk SELECT
        if t in {"address", "location", "base64"}:
            continue

        names.append(f["name"])
        types[f["name"]] = t

    return names, types

def _fetch_records_in_chunks(sf: Salesforce, sobject: str, field_names: List[str], chunk_size: int = 150) -> pd.DataFrame:
    """
    Fetchs all fields by chunking into multiple SOQL queries (Id + N fields), then joins on Id.
    """
    if "Id" not in field_names:
        field_names = ["Id"] + field_names
    chunks = _chunk(field_names, chunk_size)
    frames = []
    for i, ch in enumerate(chunks, start=1):
        # Always include Id to join reliably
        if "Id" not in ch:
            ch = ["Id"] + ch
        soql = f"SELECT {', '.join(ch)} FROM {sobject}"
        print(f"  • Query chunk {i}/{len(chunks)} ({len(ch)} fields) …")
        res = sf.query_all(soql)["records"]
        for r in res:
            r.pop("attributes", None)
        df = pd.DataFrame(res)
        frames.append(df)

    if not frames:
        # No rows returned / no chunks (edge case)
        return pd.DataFrame(columns=field_names)

    # Incrementally merge on Id
    base = frames[0]
    for f in frames[1:]:
        base = base.merge(f, on="Id", how="left", suffixes=("", "_dup"))
        # remove any accidental dup columns created by merge suffixes
        dup_cols = [c for c in base.columns if c.endswith("_dup")]
        if dup_cols:
            base.drop(columns=dup_cols, inplace=True)
    return base

def _scan_dependencies(sf: Salesforce, sobject: str, field_names: List[str]) -> pd.DataFrame:
    print("🔎 Scanning dependencies via Tooling API (Apex & Validation Rules)…")

    # Apex classes & triggers
    apex_classes = _tooling_query(sf, "SELECT Id, Name, Body FROM ApexClass")
    triggers = _tooling_query(sf, "SELECT Id, Name, Body FROM ApexTrigger")

    # Validation Rules (list basics first; formula comes from Metadata)
    vr_list = _tooling_query(
        sf,
        f"SELECT Id, ValidationName, Active, EntityDefinition.QualifiedApiName "
        f"FROM ValidationRule WHERE EntityDefinition.QualifiedApiName = '{sobject}'"
    )

    rows = []
    # Compile patterns once
    patterns = {
        fld: [
            re.compile(rf"\b{re.escape(fld)}\b", re.IGNORECASE),
            re.compile(rf"\b{re.escape(sobject)}\s*\.\s*{re.escape(fld)}\b", re.IGNORECASE),
        ]
        for fld in field_names
    }

    def _find_hits(text: str, fld: str) -> bool:
        return any(p.search(text or "") for p in patterns[fld])

    # ApexClass
    for apx in apex_classes:
        body = apx.get("Body") or ""
        for fld in field_names:
            if _find_hits(body, fld):
                rows.append({"Field API Name": fld, "Referenced In": "ApexClass",
                             "Name": apx.get("Name"), "Active": ""})

    # ApexTrigger
    for trg in triggers:
        body = trg.get("Body") or ""
        for fld in field_names:
            if _find_hits(body, fld):
                rows.append({"Field API Name": fld, "Referenced In": "ApexTrigger",
                             "Name": trg.get("Name"), "Active": ""})

    # Validation Rules: fetch metadata per rule to get the formula
    for vr in vr_list:
        vr_id = vr.get("Id")
        vr_name = vr.get("ValidationName")
        vr_active = vr.get("Active")

        # REST by sObject id
        vr_full = sf.restful(f"tooling/sobjects/ValidationRule/{vr_id}")
        meta = (vr_full or {}).get("Metadata", {})  # dict with 'errorConditionFormula' etc.
        formula = meta.get("errorConditionFormula", "") or ""

        # Now scan the formula for each field
        for fld in field_names:
            if _find_hits(formula, fld):
                rows.append({"Field API Name": fld, "Referenced In": "ValidationRule",
                             "Name": vr_name, "Active": vr_active})

    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["Field API Name", "Referenced In", "Name", "Active"]
    )

def _build_usage_frame(df: pd.DataFrame, field_types: Dict[str, str]) -> pd.DataFrame:
    if df.empty:
        print("ℹ️  No records returned. Usage will show 0% populated for all fields.")
        # Create empty frame with expected headers
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

def _mark_safe_to_delete(usage: pd.DataFrame, deps: pd.DataFrame, min_percent_populated: float) -> pd.DataFrame:
    # deps may be empty but already has the column headers; groupby will work
    dep_counts = deps.groupby("Field API Name").size().rename("Dependency Count") if not deps.empty else pd.Series(dtype=int)
    out = usage.merge(dep_counts, on="Field API Name", how="left")
    out["Dependency Count"] = out["Dependency Count"].fillna(0).astype(int)

    def _safe(row):
        name = row["Field API Name"]
        if name in PROTECTED_FIELDS:
            return False
        if row["Dependency Count"] > 0:
            return False
        return row["Percent Populated"] < min_percent_populated

    out["Safe to Delete?"] = out.apply(_safe, axis=1)
    return out

def generate_usage_report(
    username: str,
    password: str,
    token: str,
    domain: str,
    sobject: str,
    file_type: str,
    run_dependency_scan: bool = True,
    min_percent_populated: float = 5.0
):
    try:
        print(f"\n🔗 Connecting to {domain}.salesforce.com …")
        sf = Salesforce(username=username, password=password, security_token=token, domain=domain)

        print(f"🧭 Describing {sobject} fields …")
        field_names, field_types = _fetch_field_describe(sf, sobject)
        if not field_names:
            print("No fields found.")
            return

        print(f"🧮 Fetching records with chunked SOQL (this may take a bit on large objects) …")
        df = _fetch_records_in_chunks(sf, sobject, field_names)

        print("📊 Computing usage metrics …")
        usage = _build_usage_frame(df, field_types)

        if run_dependency_scan:
            deps = _scan_dependencies(sf, sobject, usage["Field API Name"].tolist() if not usage.empty else field_names)
        else:
            deps = pd.DataFrame(columns=["Field API Name", "Referenced In", "Name", "Active"])

        print("🧹 Applying safe-to-delete heuristic …")
        usage_with_flags = _mark_safe_to_delete(usage, deps, min_percent_populated)

        # Summary sheet
        total_fields = len(usage_with_flags)
        low_pop_count = int((usage_with_flags["Percent Populated"] < min_percent_populated).sum()) if not usage_with_flags.empty else 0
        with_deps = int((usage_with_flags["Dependency Count"] > 0).sum()) if not usage_with_flags.empty else 0
        safe_to_delete = int(usage_with_flags["Safe to Delete?"].sum()) if not usage_with_flags.empty else 0
        summary = pd.DataFrame([
            {"Metric": "Total Records Scanned", "Value": len(df)},
            {"Metric": "Total Fields", "Value": total_fields},
            {"Metric": f"Fields < {min_percent_populated}% populated", "Value": low_pop_count},
            {"Metric": "Fields with Dependencies", "Value": with_deps},
            {"Metric": "Safe to Delete (heuristic)", "Value": safe_to_delete},
        ])

        filename = f"{sobject.lower()}_field_deprecation_assessment.{ 'xlsx' if file_type == 'excel' else 'csv' }"
        print(f"💾 Saving to {filename} …")

        if file_type == "excel":
            with pd.ExcelWriter(filename, engine="openpyxl") as writer:
                summary.to_excel(writer, sheet_name="Summary", index=False)
                # Minor sort tweak: show "safe" first, fewest deps, lowest usage
                usage_with_flags.sort_values(
                    by=["Safe to Delete?", "Dependency Count", "Percent Populated"],
                    ascending=[False, True, True]
                ).to_excel(writer, sheet_name="FieldUsage", index=False)
                deps.sort_values(["Field API Name", "Referenced In", "Name"]).to_excel(writer, sheet_name="Dependencies", index=False)
        else:
            # CSV: write the main table; also emit sidecar CSVs
            usage_with_flags.to_csv(filename, index=False)
            deps.to_csv(f"{sobject.lower()}_dependencies.csv", index=False)
            summary.to_csv(f"{sobject.lower()}_summary.csv", index=False)

        print("✅ Report generation complete.")

    except (SalesforceGeneralError, SalesforceMalformedRequest) as e:
        print("❌ Salesforce error:", getattr(e, "content", e))
    except Exception as e:
        print("❌ Unexpected error:", e)
