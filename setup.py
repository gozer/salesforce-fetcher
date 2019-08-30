from setuptools import setup

setup(
    name='salesforce-fetcher',
    version='0.1.0',
    py_modules=['fetcher'],
    python_requires='>=2.7',
    install_requires=[
        'Click',
        'simple-salesforce',
        'pyOpenSSL>=0.14',
        'pyyaml>=3.12'
    ],
    data_files = [('salesforce-fetcher/queries',[
      'queries/contact_history.soql',
      'queries/contacts.soql',
      'queries/contact_updates.soql',
      'queries/contact_fields.yaml',
      'queries/foundation_signups.soql',
      'queries/petition_signups.soql',
    ])],
    author='Aaron Wirick',
    author_email='awirick@mozilla.com',
    description='Python tool for fetching bulk queries and reports from Salesforce',
    entry_points='''
        [console_scripts]
        salesforce-fetcher=fetcher:run
    ''',
)

