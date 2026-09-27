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

2. **Credentials are not a token.** GroupMe authenticates with a static bearer token dropped in a config file. Signal has no such thing. A signal-cli credential is a *mutable* data directory holding the account's identity key, prekeys and per-recipient ratchet state, backed by a WAL-mode SQLite database. `config_signal.json` therefore carries account *identity and location* only (`signal_number`, `account_store`); the store itself is created once by a human, via `link` or `register`+`verify`, and never by an automated run. Both the local and cloud paths chmod the extracted store directory to `0700` -- it holds the account's identity private key.

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

Both makers deploy to Google Cloud on the same trigger chain. Google renamed Cloud Functions 2nd gen to *Cloud Run functions*; 1st gen is legacy.

```
Cloud Scheduler ──07:00──▶ Pub/Sub topic ──▶ Eventarc trigger ──▶ Cloud Run
 (cron + time zone)        (autogroupchat)     (push, ack 600 s)    ├─ GroupMe: function, built from source
                                                                    └─ Signal: prebuilt container image
```

Signal cannot be a source-built function: signal-cli's native build is 110 MB gzipped, and it needs a stateful account store (see [Signal](#google-cloud-deployment-signal)). It runs as a Cloud Run *service* from an image instead. Same console, same free tier.

Commands below were checked against Google's docs on 2026-09-26 but **not executed** -- `gcloud` was unavailable where this was written. Run each step's check before moving on.

### Cost

One run a day fits the Always Free tier. Worst case, 30 runs x 540 s:

| Resource | Monthly use | Free | Share |
|---|---|---|---|
| Cloud Run vCPU | 30 x 540 s x 1 vCPU = 16,200 vCPU-s | 180,000 | 9% |
| Cloud Run memory | 30 x 540 s x 1 GiB = 16,200 GiB-s | 360,000 | 4.5% |
| Artifact Registry | ~217 MB per Signal image version | 0.5 GB | 43% per version |
| Cloud Storage | store size x versions kept | 5 GB-months, US regions only | depends |
| Secret Manager | 3 active versions (Signal) | 6 versions, 10,000 reads | 50% |

Three things can leave the free tier, each guarded in the steps below: old image versions (cleanup policy), one noncurrent store version per day (lifecycle rule), and stale secret versions (destroy them after an update). Use a US region such as `us-central1` for the storage tier.

### Shared setup (once per project)

Skip steps 4-5 if you are migrating and the topic and Scheduler job already exist.

1. Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install), then:

    ```bash
    gcloud auth login
    gcloud config set project <project>
    PROJECT=<project>
    REGION=us-central1
    SA=autogroupchat@${PROJECT}.iam.gserviceaccount.com
    ```

2. Enable the APIs:

    ```bash
    gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
      artifactregistry.googleapis.com eventarc.googleapis.com pubsub.googleapis.com \
      cloudscheduler.googleapis.com secretmanager.googleapis.com storage.googleapis.com \
      sheets.googleapis.com drive.googleapis.com
    ```

3. Create the runtime service account. It runs the service and authenticates the trigger. It is **not** what reads the spreadsheet -- that is the key in `config_googleapi.json` (see [Google Sheets](#google-sheets)).

    ```bash
    gcloud iam service-accounts create autogroupchat
    ```

4. Create the topic:

    ```bash
    gcloud pubsub topics create autogroupchat
    ```

5. Create the daily job. `--time-zone` defaults to UTC; set yours.

    ```bash
    gcloud scheduler jobs create pubsub autogroupchat-daily \
      --location=$REGION --schedule="0 7 * * *" --time-zone="America/New_York" \
      --topic=autogroupchat --message-body="run"
    ```

    Weekly instead: `--schedule="0 7 * * SUN"`. A job name cannot be reused, even after deletion.

### Google Cloud deployment (GroupMe)

1. **Configs.** Populate `configs/config_googlesheets_groupme.json`, `configs/config_googleapi.json` and `configs/config_groupme.json`. Start from a working config, not `configs_templates/config_googlesheets_groupme.json` -- that template is stale (Known issue 3). Paths inside it must carry the `configs/` prefix.

2. **Stage the source.** `gcloud run deploy --source` honours `.gitignore`, which excludes `configs/*`; deploying from the repo root would ship without configs. Stage a clean directory:

    ```bash
    STAGE=$(mktemp -d)
    cp google_cloud_main.py "$STAGE/main.py"
    cp requirements.txt "$STAGE/"
    rsync -a --exclude __pycache__ autogroupchat configs "$STAGE/"
    ```

    The configs travel inside the source upload and the built image, as they did on 1st gen. Anyone with read on the project's `run-sources-*` bucket or its Artifact Registry repo can read the GroupMe token and the Sheets key.

3. **Deploy.** `google_cloud_main.py` uses the legacy `(event, context)` signature. `GOOGLE_FUNCTION_SIGNATURE_TYPE=event` tells the buildpack to keep it, as the Signal image does with `--signature-type=event`. This combination is not documented end to end by Google; step 6 is the check.

    ```bash
    gcloud run deploy autogroupchat-groupme \
      --source "$STAGE" --function autogroupchat_pubsub --base-image python314 \
      --set-build-env-vars GOOGLE_FUNCTION_SIGNATURE_TYPE=event \
      --region $REGION --service-account $SA --no-allow-unauthenticated \
      --max-instances 1 --timeout 540
    rm -rf "$STAGE"
    ```

    `--max-instances 1`: two overlapping runs would create every group twice. `--timeout 540` stays below the 600 s ack deadline set in step 5.

4. **Let the trigger invoke it.**

    ```bash
    gcloud run services add-iam-policy-binding autogroupchat-groupme \
      --region $REGION --member=serviceAccount:$SA --role=roles/run.invoker
    ```

    Without this the trigger reports healthy but every delivery fails as unauthenticated.

5. **Create the trigger**, then raise its ack deadline:

    ```bash
    gcloud eventarc triggers create autogroupchat-groupme \
      --location=$REGION \
      --destination-run-service=autogroupchat-groupme --destination-run-region=$REGION \
      --event-filters="type=google.cloud.pubsub.topic.v1.messagePublished" \
      --transport-topic=projects/$PROJECT/topics/autogroupchat \
      --service-account=$SA --max-retry-attempts=1

    SUB=$(gcloud eventarc triggers describe autogroupchat-groupme --location=$REGION \
      --format='value(transport.pubsub.subscription)')
    gcloud pubsub subscriptions update "$SUB" --ack-deadline=600
    ```

    - Ack deadline: Eventarc defaults to 10 s. A run longer than that is redelivered and runs again -- duplicate groups. 600 s is Pub/Sub's maximum and exceeds `--timeout 540`.
    - `--max-retry-attempts=1`: no retry after a failure, matching 1st gen's default. A retried run would re-create the groups that succeeded.
    - If the Pub/Sub service agent in your project predates 2021-04-08, also grant `service-<project_number>@gcp-sa-pubsub.iam.gserviceaccount.com` `roles/iam.serviceAccountTokenCreator`.

6. **Test.** `gcloud pubsub topics publish autogroupchat --message=test`, then read the service's logs (Console > Cloud Run > `autogroupchat-groupme` > Logs). A `TypeError` about handler arguments means the signature setting in step 3 did not take; switch the handler to the `@functions_framework.cloud_event` form.

7. **Alert on errors.** Console > Logging > Logs Explorer, query `resource.labels.service_name="autogroupchat-groupme" severity>=ERROR`, then *Create alert*: pick a notification frequency, an autoclose duration and a channel (*Manage notification channels* to add email). Manage later at https://console.cloud.google.com/monitoring/alerting/policies.

8. **Retire the 1st gen function** once a scheduled run succeeds. `python310` -- the runtime it was built on -- is deprecated on 2026-10-04.

    ```bash
    gcloud functions delete <old_function_name> --region=<old_region>
    ```

    Alternative for an existing function: Google's in-place 1st gen upgrade tool (GA 2026-08-10), which keeps the name, code and configuration.

### Google Cloud deployment (Signal)

**Why not a function.**

- signal-cli's native (GraalVM, no-JRE) build is one static file: 372,377,528 bytes uncompressed, 110 MB gzipped, pinned by sha256 in `deploy/signal/Dockerfile`. Cloud Functions 1st gen caps source at 100 MB *compressed*; the artifact ships gzipped already, so no repackaging helps. The image (949 MB on disk, ~217 MB gzipped) is not subject to that limit.
- The account store signal-cli keeps is WAL-mode SQLite. WAL needs shared-memory locking that GCSFuse does not provide; mounting the data dir from GCS risks a corrupted identity store. The store runs on the container's local filesystem and moves to and from GCS as an opaque tarball.

1. **Artifact Registry repo**, with a cleanup policy keeping the two newest image versions (2 x 217 MB fits the 0.5 GB free tier; a third does not):

    ```bash
    gcloud artifacts repositories create autogroupchat --repository-format=docker --location=$REGION

    cat > policy.json <<'EOF'
    [
      {"name": "keep-2", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
      {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"tagState": "any", "olderThan": "30d"}}
    ]
    EOF
    gcloud artifacts repositories set-cleanup-policies autogroupchat --location=$REGION --policy=policy.json --dry-run
    gcloud artifacts repositories set-cleanup-policies autogroupchat --location=$REGION --policy=policy.json --no-dry-run
    ```

    A Keep rule alone deletes nothing; it only exempts versions from the Delete rule. Policies take effect within about a day.

2. **Build and push.** `--platform linux/amd64` is required on Apple Silicon: the Dockerfile fetches the x86-64 signal-cli and Cloud Run runs amd64.

    ```bash
    IMAGE=$REGION-docker.pkg.dev/$PROJECT/autogroupchat/autogroupchat-signal:v1
    gcloud auth configure-docker $REGION-docker.pkg.dev
    docker build --platform linux/amd64 -f deploy/signal/Dockerfile -t $IMAGE .
    docker push $IMAGE
    ```

3. **State bucket**: dedicated, versioned, noncurrent versions deleted after 30 days. Every run uploads the store, so versioning adds one noncurrent version per day; the lifecycle rule caps storage at about 30 x the store size (`doctor` reports it).

    ```bash
    BUCKET=<state_bucket_name>
    gcloud storage buckets create gs://$BUCKET --location=$REGION --uniform-bucket-level-access
    gcloud storage buckets update gs://$BUCKET --versioning

    cat > lc.json <<'EOF'
    {"lifecycle": {"rule": [{"action": {"type": "Delete"}, "condition": {"daysSinceNoncurrentTime": 30}}]}}
    EOF
    gcloud storage buckets update gs://$BUCKET --lifecycle-file=lc.json

    gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
      --member=serviceAccount:$SA --role=roles/storage.objectAdmin
    ```

    **Lock this bucket down before anything lands in it.** The object it holds is the account's identity private key and ratchet state, not a token -- **read access to it is full control of the Signal account**, and there is nothing to rotate afterwards. The service account needs `objectAdmin` (the store object and its lock are read, written and overwritten). Use this bucket for nothing else, and grant no human user read on it.

4. **Link and upload the store, once.** Linking needs a human scanning a QR code, so it runs locally (steps in [Signal](#signal)), never in the container:

    ```bash
    python -m autogroupchat.makers.automakesignal -g configs/config_signal.json link --name autogroupchat
    python -m autogroupchat.makers.automakesignal -g configs/config_signal.json doctor

    umask 077   # the tarball contains the account's identity private key
    tar -czf store.tar.gz -C <local_data_dir> .
    gcloud storage cp store.tar.gz gs://$BUCKET/signal-cli/<+15551234567>.tar.gz
    shred -u store.tar.gz   # or `rm -P` / `rm`; do not leave it in your CWD
    ```

5. **Configs as secrets.** The image contains no `configs/` -- the `COPY` list in `deploy/signal/Dockerfile` never names it, which keeps the Sheets key out of every layer. Cloud Run mounts each secret in its own directory: it cannot put two secrets in one directory, and a mount hides whatever the directory held. So each file gets its own path:

    | Secret | Mounted at | Source |
    |---|---|---|
    | `signal-scraper-config` | `/secrets/scraper/config.json` | `configs_templates/config_googlesheets_signal.json` |
    | `googleapi-sa-key` | `/secrets/googleapi/config_googleapi.json` | your Sheets service-account key |
    | `signal-config` | `/secrets/signal/config_signal.json` | `configs_templates/config_signal_gcs.json` |

    The scraper config opens the other two by the paths written inside it, so those must be absolute:

    ```json
    "api_config": "/secrets/googleapi/config_googleapi.json",
    "group_creation_config": "/secrets/signal/config_signal.json"
    ```

    Then:

    ```bash
    gcloud secrets create signal-scraper-config --data-file=<scraper_config.json>
    gcloud secrets create googleapi-sa-key --data-file=<config_googleapi.json>
    gcloud secrets create signal-config --data-file=<config_signal_gcs.json>
    for s in signal-scraper-config googleapi-sa-key signal-config; do
      gcloud secrets add-iam-policy-binding $s \
        --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor
    done
    ```

    Do not pass `--location`: Cloud Run does not support regional secrets. To change one later, `gcloud secrets versions add <name> --data-file=...`, then `gcloud secrets versions destroy <old_version> --secret=<name>` -- only 6 active versions are free.

6. **Deploy:**

    ```bash
    gcloud run deploy autogroupchat-signal \
      --image $IMAGE --region $REGION --service-account $SA --no-allow-unauthenticated \
      --max-instances 1 --concurrency 1 --timeout 540 --memory 1Gi \
      --set-env-vars AUTOGROUPCHAT_CONFIG=/secrets/scraper/config.json \
      --set-secrets=/secrets/scraper/config.json=signal-scraper-config:latest,/secrets/googleapi/config_googleapi.json=googleapi-sa-key:latest,/secrets/signal/config_signal.json=signal-config:latest
    ```

    - `--max-instances 1` and `--concurrency 1` are **correctness requirements, not tuning**. Two invocations against one account store will corrupt it.
    - `--timeout 540`: `invocation_budget_seconds` defaults to 480 (540 minus 60 s for moving the store), so the maker fails inside its own budget instead of being killed mid-write. 540 also stays below the 600 s ack deadline (step 7).
    - `--memory 1Gi` is **provisional**, not derived. `/tmp` is tmpfs, so the extracted store counts against memory. Set it from what `doctor` reports the store weighs, plus headroom.

7. **Trigger.** Same as GroupMe steps 4-5, with `autogroupchat-signal` for the service and trigger names:

    ```bash
    gcloud run services add-iam-policy-binding autogroupchat-signal \
      --region $REGION --member=serviceAccount:$SA --role=roles/run.invoker

    gcloud eventarc triggers create autogroupchat-signal \
      --location=$REGION \
      --destination-run-service=autogroupchat-signal --destination-run-region=$REGION \
      --event-filters="type=google.cloud.pubsub.topic.v1.messagePublished" \
      --transport-topic=projects/$PROJECT/topics/autogroupchat \
      --service-account=$SA --max-retry-attempts=1

    SUB=$(gcloud eventarc triggers describe autogroupchat-signal --location=$REGION \
      --format='value(transport.pubsub.subscription)')
    gcloud pubsub subscriptions update "$SUB" --ack-deadline=600
    ```

    The ack deadline matters more here: a redelivery during a run hits `--max-instances 1`, is rejected, and is retried until it runs a second time after the first finishes. If GroupMe is retired, delete its trigger (`gcloud eventarc triggers delete autogroupchat-groupme --location=$REGION`) or both makers will fire on the same topic.

8. **Test** with `gcloud pubsub topics publish autogroupchat --message=test` and read the service's logs. functions-framework adapts Eventarc's CloudEvent into the legacy `autogroupchat_pubsub(event, context)` handler -- confirmed by running the built container locally. Add an error alert as in GroupMe step 7, with `service_name="autogroupchat-signal"`.

#### Recovering a desynchronised Signal account

`GcsStore` cannot make a message send and the store upload atomic. If the container is hard-killed between sending a message and uploading the store, GCS retains the *pre-run* snapshot while the recipients' Double Ratchet state has already advanced from the message that was actually sent. Later messages to those recipients can then fail to decrypt.

`--max-instances 1` / `--concurrency 1`, uploading in a `finally` block, and the lock-plus-generation check on every upload reduce this to a crash-only failure -- but a hard kill in exactly that window is not fully removable. It is the cost of running a stateful Signal client in an ephemeral container, not a bug to fix here.

Recovery is manual:

1. Check whether the bucket holds a newer object version than the one in use: `gcloud storage ls --all-versions gs://<state_bucket_name>/signal-cli/<+15551234567>.tar.gz`.
2. If there is no newer version, re-link the device with the same config -- this replaces the store: `python -m autogroupchat.makers.automakesignal -g configs/config_signal.json link --name autogroupchat`. Then upload it as in Signal step 4.
3. Re-linking changes nothing about group membership. Groups persist server-side; only this device's local ratchet state is reset.

## Resources

 * API documentation for GroupMe Python project: https://groupy.readthedocs.io/en/latest/pages/api.html
 * Getting data from Google Sheets: https://developers.google.com/sheets/api/quickstart/python
 * Running a python script in the cloud automatically: https://towardsdatascience.com/how-to-schedule-a-python-script-on-google-cloud-721e331a9590
 * Accessing Google Sheets from within a Google Function: https://stackoverflow.com/a/51037780

 * Build Google Cloud Function that runs python code: https://cloud.google.com/functions/docs/create-deploy-http-python#linux-or-mac-os-x
 * https://codelabs.developers.google.com/codelabs/intelligent-gmail-processing/
