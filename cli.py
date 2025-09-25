import questionary
from cleaner import generate_usage_report

def main():
    print("\n🔧 Salesforce Field Deprecation CLI\n")

    username = questionary.text("Salesforce Username:").ask()
    password = questionary.password("Salesforce Password:").ask()
    token = questionary.text("Salesforce Security Token:").ask()
    is_sandbox = questionary.confirm("Is this a Sandbox org?").ask()

    sobject = questionary.text("Which SObject do you want to analyze? (e.g., Contact, Account)").ask()
    file_type = questionary.select("Choose file output format:", choices=["Excel", "CSV"]).ask()
    run_dep_scan = questionary.confirm("Scan dependencies (Apex/Triggers/Validation Rules)?").ask()
    min_pct = questionary.text("Min % populated below which a field is considered low-use (default 5):").skip_if(lambda v: False).ask()

    try:
        min_pct_val = float(min_pct) if min_pct else 5.0
    except ValueError:
        min_pct_val = 5.0

    generate_usage_report(
        username=username,
        password=password,
        token=token,
        domain="test" if is_sandbox else "login",
        sobject=sobject,
        file_type=file_type.lower(),
        run_dependency_scan=run_dep_scan,
        min_percent_populated=min_pct_val
    )

if __name__ == "__main__":
    main()
