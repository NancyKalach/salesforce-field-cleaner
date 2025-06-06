# Salesforce Field Usage Cleaner

Identify and export unused or underused Salesforce fields at scale using a simple Python CLI tool.

This tool connects to your Salesforce org, analyzes the data population across fields on any SObject, and exports a usage report in Excel or CSV format. Great for org cleanup, technical debt reduction, and data quality assessments.

## Features

* Securely connect to any Salesforce org (Production or Sandbox)
* Analyze any standard or custom SObject
* Get field usage stats: null counts, usage %, and more
* Export to Excel (`.xlsx`) or CSV (`.csv`)
* Runs entirely from your terminal

## Installation

1. Clone the repo:

   git clone https://github.com/your-username/salesforce-field-cleaner.git
   cd salesforce-field-cleaner

2. Set up your environment:

   python -m venv venv
   source venv/bin/activate  # For Windows: venv\Scripts\activate
   pip install -r requirements.txt


## Credentials

Create a '.env' file or provide credentials directly in the CLI when prompted.

Alternatively, copy and fill this template:

SF_USERNAME=your_email@example.com  
SF_PASSWORD=your_password  
SF_TOKEN=your_security_token  

⚠️ Never commit '.env' files to version control.

## Usage

Just run the below command:

python cli.py

You’ll be prompted to:

* Enter your Salesforce login credentials
* Indicate if it’s a Sandbox
* Choose an SObject (e.g., 'Contact', 'Account', 'Custom_Object__c')
* Pick your output format (Excel or CSV)


## Sample Output

| Field API Name | Null Count | Populated Count | Percent Populated |
| -------------- | ---------- | --------------- | ----------------- |
| Fax            | 9832       | 168             | 1.68%             |
| Email          | 500        | 9500            | 95.00%            |

Output will be saved as:

contact_field_population_report.xlsx

## License

MIT License © \Nancy Al Kalach