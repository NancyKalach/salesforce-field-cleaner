import questionary
from cleaner import generate_usage_report

def main():
    print("\n Salesforce Field Usage Cleaner\n")

    username = questionary.text("Salesforce Username:").ask()
    password = questionary.password("Salesforce Password:").ask()
    token = questionary.text("Salesforce Security Token:").ask()
    is_sandbox = questionary.confirm("Is this a Sandbox org?").ask()

    sobject = questionary.text("Which SObject do you want to analyze? (e.g. Contact, Account)").ask()
    file_type = questionary.select("Choose file output format:", choices=["Excel", "CSV"]).ask()

    generate_usage_report(
        username=username,
        password=password,
        token=token,
        domain='test' if is_sandbox else 'login',
        sobject=sobject,
        file_type=file_type.lower()
    )

if __name__ == "__main__":
    main()
