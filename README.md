# autogroupchat

Autogroupchat consists of two modular pieces: `scrapers` and `makers`. `Scrapers` are designed to get data from a spreadsheet online. The first scraper built was for Google Sheets. `Makers` are designed to create a group chat from the data scraped by the `scraper`. The first maker built was for GroupMe.

Lastly, autogroupchat is meant to be able to deploy as a function on any cloud. Initially it was implemented on Google cloud.

## Spreadsheet layout

**TODO**

## Scrapers

### Subclassing

To implement a new scraper, subclass [AutoScrapeGroup](/autogroupchat/scrapers/autoscrapegroup.py)

### Authentication

Authentication is module specific, but is intended to be provided in a `json` config file starting with the prefix `config_`. For instance, `config_googleapi.json` is the default config file for the Google Sheets plugin.

### Scraper Modules

#### [Google Sheets](/autogroupchat/scrapers/autoscrapegooglesheets.py)

Google sheets is based on the following Google tutorial: https://developers.google.com/sheets/api/quickstart/python

In order to set up authentication, move the `credentials.json` described in the tutorial to `config_googleapi.json` in the root of this project. If using 0Auth, users must run the module in order to setup the 0Auth token. If a user runs `autogroupchat/scrapers/autoscrapegooglesheets.py`, it will prompt you to login and grant initial access. It will write (by default) the token to `config_googleapi_token.json`. This can be copied into the cloud and run without user interaction. **WARNING: be careful with any of the `config_*.json` files, they will provide at least API access to your account's resources.**

## Makers

### Subclassing

To implement a new maker, subclass [AutoMakeGroupChat](/autogroupchat/makers/automakegroupchat.py)

### Authentication

Authentication is module specific, but is intended to be provided in a `json` config file starting with the prefix `config_`. For instance, `config_groupme.json` is the default config file for the GroupMe plugin.

### Maker Modules

#### [GroupMe](/autogroupchat/makers/automakegroupme.py)

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

#### [Signal](/autogroupchat/makers/automakesignal.py)

1. **Prerequisite.** [signal-cli](https://github.com/AsamK/signal-cli) v0.14.7 or newer. Use the native (GraalVM) build, not the JRE build: it needs no JRE at all, and is the version `deploy/signal/Dockerfile` bakes into the Cloud Run image.

2. **Credentials are not a token.** GroupMe authenticates with a static bearer token dropped in a config file. Signal has no such thing. A signal-cli credential is a *mutable* data directory holding the account's identity key, prekeys and per-recipient ratchet state, backed by a WAL-mode SQLite database. `config_signal.json` therefore carries account *identity and location* only (`signal_number`, `account_store`); the store itself is created once by a human, via `link` or `register`+`verify`, and never by an automated run.

3. **Bootstrap.** Link this tool as a secondary device on an existing phone number (or use `register`/`verify` instead, for a dedicated number), then check the result:

    ```
    python -m autogroupchat.makers.automakesignal -g configs/config_signal.json link --name autogroupchat
    python -m autogroupchat.makers.automakesignal -g configs/config_signal.json doctor
    ```

    `link` prints a `sgnl://` URI; scan it from Signal on your phone under Settings > Linked Devices > +. `doctor` re-checks the signal-cli version floor, confirms the account store is usable, and logs the store's real on-disk size -- the number to size `--memory` from in the Cloud Run deploy below, rather than guessing.

4. **Differences from GroupMe.** Six behaviours changed, not just the transport:

    | GroupMe | Signal |
    |---|---|
    | One owner, transferable | A set of admins; `change_group_owner` promotes to admin and we stay admin |
    | Per-group nicknames | None -- members show their own profile name, so spreadsheet names are log labels only |
    | Add succeeds or fails | Members whose profile key we lack are *invited*; they are not in the group until they accept |
    | One bad member fails only that member | One unregistered number rejects the whole batch; the maker retries per-member to isolate it |
    | `destroy()` deletes the group | No such operation -- purge leaves and forgets; the group survives for its remaining members |
    | Purge uses the creation timestamp | No timestamp exists; the creation date is stamped into the group description |

5. **Purge stamp.** `listGroups` reports no creation date, so `create_group` prefixes the description with a machine-readable stamp:

    ```
    autogroupchat:1:2026-08-31 - Group created by autogroupchat. Please contact s41l8hu2@duck.com with any issues.
    ```

    A description that does not start with this exact `autogroupchat:<version>:<YYYY-MM-DD>` prefix -- unstamped, or stamped but unparseable -- is never purged. Fail-closed on purpose: a missed purge just leaves a stale group; a false positive would abandon a live one.

#### Known issues

Found while building the Signal maker. Not fixed, per scope; recorded so they are not rediscovered the hard way.

1. `AutoMakeGroupChat.add_members_group` calls `self.add_member_group(group, name, number)`, but the ABC declares `add_member_group(self, name, phone_number)` -- an arity mismatch. Latent only because both `AutoMakeGroupMe` and `AutoMakeSignal` override `add_member_group` with a leading `group` parameter, so the ABC's own (wrong) signature is never actually invoked.
2. `AutoMakeGroupChat.group_startup` is not a `@classmethod`, yet is called as `clazz.group_startup(clazz, ...)`, passing the class as both the implicit and an explicit first argument. `AutoMakeSignal.group_startup` follows this same convention rather than fixing it.
3. `configs_templates/config_googlesheets_groupme.json` is stale against the code: it omits `worksheet`, which `scrape_using_dict` reads unconditionally as `args['worksheet']`, and its `api_config` / `group_creation_config` paths lack the `configs/` prefix the working config uses. Copying it as-is produces `KeyError: 'worksheet'`. `configs_templates/config_googlesheets_signal.json` is shaped from the working config instead -- do not use the GroupMe template as a starting point for a new scraper config.
4. The base `group_startup` asserts `len(admin) == 1 and "Only one owner is allowed per group."` -- the `and "string"` is always truthy, so this asserts nothing beyond the length, and the message never fires. `AutoMakeSignal.group_startup` uses the correct `assert cond, "message"` form instead.
5. `group_startup` consumes its `admin` dict via `.popitem()`. A batch caller that reuses one `admin` dict across several `group_startup` calls will find admin promotion silently skipped from the second group onward. Inherited from the base class, not introduced by the Signal maker, but easy to trip over.

## Cloud deployment

### Google Cloud deployment

Cloud deployment was initially done based on this tutorial: https://towardsdatascience.com/how-to-schedule-a-python-script-on-google-cloud-721e331a9590. Reference this tutorial if setting up autogroupchat for the first time.

The tutorial goes through these rough steps:
1. Enable Google Cloud Platform (GCP)
2. Schedule a job with Google Cloud Scheduler

    2.1. Timing: schedule the job for daily at 7am using the cron line `0 7 * * *`or every sunday at 7am using the cron line `0 7 * * SUN`.
    
    2.2. Target: Pub/Sub. The topic prefix will be set based on the project, the sub-topic should be `test` or `prod` or something else based on your use case.
    
    2.3. Advanced: optional retry settings

3. Create a Google Cloud Function
    
    3.1. Tab 1 - Trigger: Pub/Sub. The topic should be the same as in 2.2.
    
    3.2. Tab 2 - Runtime: Pick the most recent Python version. Built initially using 3.10
    
    3.3. Tab 2 - main.py: Copy all of [google_cloud_main.py](/google_cloud_main.py) into `main.py`
    
    3.4. Tab 2 - requirements.txt: Copy all of [requirements.txt](/requirements.txt) into `requirements.txt`
    
    3.5. Tab 2 - configs: copy all the config files from [configs_templates](/configs_templates) folder into [configs](/configs). Make sure there are no stubbed `<>` tags in the config files-- populate them with real data.
    
    3.6. Tab 3 - configs: copy the files from the [configs](/configs) folder into the Cloud Function at the same level as `main.py`. (for google sheets --> groupme, your project should look like the screenshot below and should include configs: [config_googlesheets_groupme.json](/config_googlesheets_groupme.json), [config_googleapi_token.json.json](/config_googleapi_token.json.json), [config_googleapi.json](config_googleapi.json), and [config_groupme.json](config_groupme.json))

    ![Google Cloud Function source](/assets/images/google/google_cloud_function_source.png)

4. Setup an alert if there are errors

    4.1. Go to the `Logs` tab in the Google Cloud Functiono created above
    
    4.2. Click `View in Log Explorer`
    
    4.3. In log explorer, click `Create Alert`
    
        4.3.1. Pick a name for the alert message
        
        4.3.2. Choose logs to include in the alert (to alert on). (Recommend: `severity="ERROR"`)
        
        4.3.3. Set Notification Frequency and Autoclose Duration. Notification frequency is how often to send an alert if they are constantly erroring, Autoclose duration is when to automatically resolve the notification. 
        
        4.3.4. Set who should be notified. You can go into `Manage Notification Channels` to add a way to notify yourself.
        
        
    4.4. To manage later, go to https://console.cloud.google.com/monitoring/alerting/policies

### Google Cloud deployment (Signal, 2nd gen)

The Signal maker cannot deploy as a 1st-gen Cloud Function like the GroupMe path above. It needs a 2nd-gen Cloud Run service, built from a container image.

**Why 2nd gen is required.**

- signal-cli's native (GraalVM, no-JRE) build is one static file: 372,377,528 bytes uncompressed, 110 MB gzipped, pinned by sha256 in `deploy/signal/Dockerfile`. Cloud Functions 1st gen caps source at 100 MB *compressed* (500 MB uncompressed). The compressed limit binds here, and the artifact ships as gzip already, so no repackaging helps -- 1st gen is impossible regardless of the uncompressed figure.
- The account store signal-cli keeps is WAL-mode SQLite. WAL needs shared-memory locking that GCSFuse does not provide; mounting the data dir from GCS risks a corrupted identity store. The store must run on a real local filesystem and move to and from GCS as an opaque tarball, never as a mounted volume.

The built image itself is 949 MB uncompressed on disk (governs Cloud Run's image cache and local disk footprint) and about 217 MB gzipped over the wire (governs registry push/pull and cold-start pull time). Both numbers are separate from the 1st-gen limit above, which is a *source* size limit, not an image size limit -- 1st gen is ruled out on that basis alone, independent of how big the image ends up.

**1. The GCS state bucket**, with versioning on, so a bad upload still leaves the previous snapshot recoverable:

```bash
gsutil mb -b on gs://<state_bucket_name>
gsutil versioning set on gs://<state_bucket_name>
```

**2. Build, push and deploy:**

```bash
docker build -f deploy/signal/Dockerfile -t <region>-docker.pkg.dev/<project>/<repo>/autogroupchat-signal:v1 .
docker push <region>-docker.pkg.dev/<project>/<repo>/autogroupchat-signal:v1

gcloud run deploy autogroupchat-signal \
  --image <region>-docker.pkg.dev/<project>/<repo>/autogroupchat-signal:v1 \
  --region <region> \
  --no-allow-unauthenticated \
  --max-instances 1 \
  --concurrency 1 \
  --timeout 540 \
  --memory 1Gi
```

- `--max-instances 1` and `--concurrency 1` are **correctness requirements, not tuning**. Two invocations running against the same account store at once will corrupt it -- nothing else in this design enforces mutual exclusion at that scale.
- `--timeout 540` is the Cloud Run 2nd-gen event-driven ceiling. The config's `invocation_budget_seconds` defaults to 480 (540 minus 60 s reserved for downloading and uploading the store), so the maker fails cleanly inside its own budget instead of being killed mid-write by the platform.
- `--memory 1Gi` is **provisional**, not derived. `/tmp` is tmpfs, so the extracted account store counts against memory. Run `doctor` (above) once the account is linked, and set `--memory` from what it reports the store weighs plus headroom -- do not deploy on this guess alone.

**3. The Eventarc trigger**, on the existing Cloud Scheduler Pub/Sub topic: point its destination at the new `autogroupchat-signal` Cloud Run service instead of the GroupMe Cloud Function. Nothing about the schedule or the topic changes from the GroupMe setup described above.

functions-framework is started with `--signature-type=event` (see the `CMD` in `deploy/signal/Dockerfile`), which adapts Eventarc's CloudEvent payload into the legacy `autogroupchat_pubsub(event, context)` handler unchanged -- confirmed by running the built container and observing the adapted call reach the handler.

**4. Uploading the linked store, once**, after running `link` locally -- the account cannot be linked from inside the container; linking needs a human scanning a QR code:

```bash
tar -czf store.tar.gz -C <local_data_dir> .
gsutil cp store.tar.gz gs://<state_bucket_name>/signal-cli/<+15551234567>.tar.gz
```

After this, every deployed invocation just serves; the account is not re-linked again unless the store is lost (below).

#### Recovering a desynchronised Signal account

`GcsStore` cannot make a message send and the store upload atomic. If the container is hard-killed between sending a message and uploading the store, GCS retains the *pre-run* snapshot while the recipients' Double Ratchet state has already advanced from the message that was actually sent. Later messages to those recipients can then fail to decrypt.

`--max-instances 1` / `--concurrency 1`, uploading in a `finally` block, and the lock-plus-generation check on every upload reduce this to a crash-only failure -- but a hard kill in exactly that window is not fully removable. It is the cost of running a stateful Signal client in an ephemeral container, not a bug to fix here.

Recovery is manual:

1. Check whether the bucket holds a newer object version than the one in use: `gsutil ls -a gs://<state_bucket_name>/signal-cli/<+15551234567>.tar.gz`.
2. If there is no newer version, re-link the device with the same config -- this replaces the store: `python -m autogroupchat.makers.automakesignal -g configs/config_signal.json link --name autogroupchat`.
3. Re-linking changes nothing about group membership. Groups persist server-side; only this device's local ratchet state is reset.

## Resources

 * API documentation for GroupMe Python project: https://groupy.readthedocs.io/en/latest/pages/api.html
 * Getting data from Google Sheets: https://developers.google.com/sheets/api/quickstart/python
 * Running a python script in the cloud automatically: https://towardsdatascience.com/how-to-schedule-a-python-script-on-google-cloud-721e331a9590
 * Accessing Google Sheets from within a Google Function: https://stackoverflow.com/a/51037780

 * Build Google Cloud Function that runs python code: https://cloud.google.com/functions/docs/create-deploy-http-python#linux-or-mac-os-x
 * https://codelabs.developers.google.com/codelabs/intelligent-gmail-processing/
