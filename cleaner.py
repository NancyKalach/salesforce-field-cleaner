import pandas as pd
from simple_salesforce import Salesforce
from dotenv import load_dotenv
import os

def generate_usage_report(username, password, token, domain, sobject, file_type):
    print(f"\n🔗 Connecting to {domain}.salesforce.com...")
    sf = Salesforce(username=username, password=password, security_token=token, domain=domain)

    print(f" Fetching field metadata for {sobject}...")
    fields = sf.__getattr__(sobject).describe()['fields']
    field_names = [f['name'] for f in fields]

    query = f"SELECT {', '.join(field_names)} FROM {sobject}"
    print(f" Running query: {query[:80]}...")

    records = sf.query_all(query)['records']
    for r in records:
        r.pop('attributes', None)

    df = pd.DataFrame(records)
    total_records = len(df)

    null_counts = df.isnull().sum()
    field_usage = pd.DataFrame({
        "Field API Name": null_counts.index,
        "Null Count": null_counts.values,
        "Populated Count": total_records - null_counts.values,
        "Percent Populated": ((total_records - null_counts.values) / total_records * 100).round(2)
    })

    field_usage = field_usage.sort_values(by="Percent Populated")

    filename = f"{sobject.lower()}_field_population_report.{ 'xlsx' if file_type == 'excel' else 'csv' }"
    print(f" Saving to {filename}...")

    if file_type == 'excel':
        field_usage.to_excel(filename, index=False)
    else:
        field_usage.to_csv(filename, index=False)

    print(" Report generation complete.")