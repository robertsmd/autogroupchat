# autogroupchat

## Usage

Autogroupchat consists of two modular pieces: `scrapers` and `makers`. `Scrapers` are designed to get data from a spreadsheet online. The first scraper built was for Google Sheets. `Makers` are designed to create a group chat from the data scraped by the `scraper`. The first maker built was for GroupMe.

Lastly, autogroupchat is meant to be able to deploy as a function on any cloud. Initially it was implemented on Google cloud.

### Scrapers

#### Subclassing

To implement a new scraper, subclass [AutoScrapeGroup](/autogroupchat/scrapers/autoscrapegroup.py)

#### Authentication

Authentication is module specific, but is intended to be provided in a `json` config file starting with the prefix `config_`. For instance, `config_googleapi.json` is the default config file for the Google Sheets plugin.

#### Scraper Modules

##### [Google Sheets](/autogroupchat/scrapers/autoscrapegooglesheets.py)

Google sheets is based on the following Google tutorial: https://developers.google.com/sheets/api/quickstart/python

In order to set up authentication, move the `credentials.json` described in the tutorial to `config_googleapi.json` in the root of this project. If using 0Auth, users must run the module in order to setup the 0Auth token. If a user runs `autogroupchat/scrapers/autoscrapegooglesheets.py`, it will prompt you to login and grant initial access. It will write (by default) the token to `config_googleapi_token.json`. This can be copied into the cloud and run without user interaction. **WARNING: be careful with any of the `config_*.json` files, they will provide at least API access to your account's resources.**

### Makers

#### Subclassing

To implement a new maker, subclass [AutoMakeGroupChat](/autogroupchat/makers/automakegroupchat.py)

#### Authentication

Authentication is module specific, but is intended to be provided in a `json` config file starting with the prefix `config_`. For instance, `config_groupme.json` is the default config file for the GroupMe plugin.

#### Maker Modules

##### [GroupMe](/autogroupchat/makers/automakegroupme.py)

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

#### Google Cloud deployment

Cloud deployment was initially done based on this tutorial: https://towardsdatascience.com/how-to-schedule-a-python-script-on-google-cloud-721e331a9590. Reference this tutorial if setting up autogroupchat for the first time.

The tutorial goes through these rough steps:
1. Enable Google Cloud Platform (GCP)
2. Schedule a job with Google Cloud Scheduler

    2.1. Timing: schedule the job for daily at 7am using the cron line `0 7 * * *`)
    2.2. Target: Pub/Sub. The topic prefix will be set based on the project, the sub-topic should be `test` or `prod` or something else based on your use case.
    2.3. Advanced: optional retry settings

3. Create a Google Cloud Function
    3.1. Tab 1 - Trigger: Pub/Sub. The topic should be the same as in 2.2.
    3.2. Tab 2 - Runtime: Pick the most recent Python version. Built initially using 3.10
    3.3. Tab 2 - main.py: Copy all of [google_cloud_main.py](/google_cloud_main.py) into `main.py`
    3.4. Tab 2 - requirements.txt: Copy all of [requirements.txt](/requirements.txt) into `requirements.txt`
    3.5. Tab 2 - configs: copy all the config files from [configs_templates](/configs_templates) folder into [configs](/configs). Make sure there are no stubbed `<>` tags in the config files-- populate them with real data.
    3.6. Tab 3 - configs: copy the files from the [configs](/configs) folder into the Cloud Function at the same level as `main.py`. (for google sheets --> groupme, should include configs: [config_googlesheets_groupme.json](/config_googlesheets_groupme.json), [config_googleapi_token.json.json](/config_googleapi_token.json.json), [config_googleapi.json](config_googleapi.json), and [config_groupme.json](config_groupme.json))

    ![Google Cloud Function source](/assets/images/google/google_cloud_function_source.png)

## Resources

 * API documentation for GroupMe Python project: https://groupy.readthedocs.io/en/latest/pages/api.html
 * Getting data from Google Sheets: https://developers.google.com/sheets/api/quickstart/python
 * Running a python script in the cloud automatically: https://towardsdatascience.com/how-to-schedule-a-python-script-on-google-cloud-721e331a9590
 * Accessing Google Sheets from within a Google Function: https://stackoverflow.com/a/51037780

 * Build Google Cloud Function that runs python code: https://cloud.google.com/functions/docs/create-deploy-http-python#linux-or-mac-os-x
 * https://codelabs.developers.google.com/codelabs/intelligent-gmail-processing/
