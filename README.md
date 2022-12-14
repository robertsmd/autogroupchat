# autogroupchat

## Usage

Autogroupchat consists of two modular pieces: `scrapers` and `makers`. `Scrapers` are designed to get data from a spreadsheet online. The first scraper built was for Google Sheets. `Makers` are designed to create a group chat from the data scraped by the `scraper`. The first maker built was for GroupMe.

Lastly, autogroupchat is meant to be able to deploy as a function on any cloud. Initially it was implemented on Google cloud.

### Scrapers

#### Subclassing

To implement a new scraper, subclass [AutoScrapeGroup](autogroupchat/scrapers/autoscrapegroup.py)

#### Authentication

Authentication is module specific, but is intended to be provided in a `json` config file starting with the prefix `config_`. For instance, `config_googleapi.json` is the default config file for the Google Sheets plugin.

### Makers

#### Subclassing

To implement a new maker, subclass (AutoMakeGroupChat)[autogroupchat/makers/automakegroupchat.py]

#### Authentication

Authentication is module specific, but is intended to be provided in a `json` config file starting with the prefix `config_`. For instance, `config_groupme.json` is the default config file for the GroupMe plugin.

#### Modules

##### GroupMe

1. API access to GroupMe starts by going to https://dev.groupme.com.

2. Click `login` in the top right corner, or go to https://dev.groupme.com/session/new and login with your username/email/phone number and password. This will require a 2FA PIN confirmation to your phone number on the account.
![Login Screen!](/assets/images/groupme/groupme_login.png)



3. Once logged in, you can see documentation of the HTTP REST API. Alternately, you can use a python wrapper. This module used `groupy` https://groupy.readthedocs.io/en/latest/pages/api.html.

4. Click "Access Token" in the top right corner.
![Home](/assets/images/groupme/groupme_logged_in.png)

5. Copy the access token *(WARNING: Don't share this. Someone with this token can act on your behalf on GroupMe.)*
![Access Token](/assets/images/groupme/groupme_access_token.png)

6. Paste the access token into `config_groupme.json`
![Config File](/assets/images/groupme/groupme_config_file.png)

### Cloud deployment

## Resources

 * API documentation for GroupMe Python project: https://groupy.readthedocs.io/en/latest/pages/api.html
 * Getting data from Google Sheets: https://developers.google.com/sheets/api/quickstart/python
 * Running a python script in the cloud automatically: https://towardsdatascience.com/how-to-schedule-a-python-script-on-google-cloud-721e331a9590
 * Accessing Google Sheets from within a Google Function: https://stackoverflow.com/a/51037780

 * Build Google Cloud Function that runs python code: https://cloud.google.com/functions/docs/create-deploy-http-python#linux-or-mac-os-x
 * https://codelabs.developers.google.com/codelabs/intelligent-gmail-processing/
