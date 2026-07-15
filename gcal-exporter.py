#!/usr/bin/python3
# pylint: disable=invalid-name
# Module filename keeps the historical CLI/container name (gcal-exporter).
"""
Checks Google Calendar and exports metrics via prometheus.
"""

import datetime
import json
import logging
import os
import sys
from datetime import timedelta
from threading import Thread
from time import sleep

import configargparse
import httplib2
import waitress
from dateutil.parser import parse as parsedate
from flask import Flask, Response
from googleapiclient import discovery
from oauth2client import client
from oauth2client.file import Storage
from prometheus_client import Gauge, make_wsgi_app
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from gcal_classify import (
    attendee_buckets,
    build_color_map,
    classify_event,
    parse_class_pairs,
)

app = Flask("prometheus-gcal-exporter")

# Set in __main__ / main(); declared here so pylint knows they exist.
args = None
GCAL_CLIENT = None
_readiness = [""]

gauge_mins_external = Gauge('gcal_mins_external', 'Date metrics', ['date'])
gauge_count_external = Gauge('gcal_count_external', 'Date metrics', ['date'])

gauge_mins_internal = Gauge('gcal_mins_internal', 'Date metrics', ['date'])
gauge_count_internal = Gauge('gcal_count_internal', 'Date metrics', ['date'])

gauge_mins_used = Gauge('gcal_mins_used', 'Date metrics', ['date'])
gauge_mins_available = Gauge('gcal_mins_available', 'Date metrics', ['date'])
gauge_count = Gauge('gcal_count', 'Date metrics', ['date'])

gauge_mins_by_class = Gauge(
    'gcal_mins_by_class',
    'Minutes by meeting class',
    ['date', 'class'],
)
gauge_count_by_class = Gauge(
    'gcal_count_by_class',
    'Event count by meeting class',
    ['date', 'class'],
)
gauge_attendees = Gauge(
    'gcal_attendees',
    'Attendee RSVP counts by optionality and response',
    ['date', 'optionality', 'response'],
)

_ALL_GAUGES = (
    gauge_mins_external,
    gauge_count_external,
    gauge_mins_internal,
    gauge_count_internal,
    gauge_mins_used,
    gauge_mins_available,
    gauge_count,
    gauge_mins_by_class,
    gauge_count_by_class,
    gauge_attendees,
)


def get_file_path(filename):
    """Return a path under ~/.prometheus-gcal-exporter, creating the dir if needed."""
    config_dir = os.path.join(os.path.expanduser("~"), ".prometheus-gcal-exporter")

    if not os.path.exists(config_dir):
        os.mkdir(config_dir)

    path = os.path.join(config_dir, filename)

    logging.info("get_file_path %s exists: %s", path, os.path.exists(path))

    return path


def get_credentials():
    """Gets valid user credentials from storage.

    If nothing has been stored, or if the stored credentials are invalid,
    the OAuth2 flow is completed to obtain the new credentials.
    """
    while not os.path.exists(args.clientSecretFile):
        set_readiness("Waiting for client secret file")
        logging.fatal(
            "Client secrets file does not exist: %s . "
            "You probably need to download this from the Google API console.",
            args.clientSecretFile,
        )
        sleep(10)

    credentials_path = args.credentialsPath

    store = Storage(credentials_path)
    credentials = store.get()

    if not credentials or credentials.invalid:
        scopes = 'https://www.googleapis.com/auth/calendar.readonly'

        flow = client.flow_from_clientsecrets(args.clientSecretFile, scopes)
        flow.user_agent = 'prometheus-gcal-exporter'

        credentials = run_flow(flow, store)

        logging.info("Storing credentials to %s", credentials_path)

    return credentials


def run_flow(flow, store):
    """Run the OAuth out-of-band flow and persist credentials."""
    flow.redirect_uri = client.OOB_CALLBACK_URN
    authorize_url = flow.step1_get_authorize_url()

    logging.info("Go and authorize at: %s", authorize_url)

    if sys.stdout.isatty():
        code = input('Enter code:').strip()
    else:
        logging.info("Waiting for code at %s", get_file_path('auth_code'))

        while True:
            try:
                if os.path.exists(get_file_path('auth_code')):
                    with open(
                        get_file_path('auth_code'),
                        'r',
                        encoding='utf-8',
                    ) as auth_code_file:
                        code = auth_code_file.read()
                        break

            except OSError as err:
                logging.critical(err)

            set_readiness("Waiting for auth code")
            sleep(10)
    try:
        credential = flow.step2_exchange(code, http=None)
    except client.FlowExchangeError as err:
        logging.fatal("Auth failure: %s", str(err))
        sys.exit(1)

    set_readiness("")

    store.put(credential)
    credential.set_store(store)

    return credential


def clear_events_cache():
    """Remove cached event JSON files under /opt/events."""
    if not os.path.isdir('/opt/events'):
        return
    for filename in os.listdir('/opt/events'):
        os.unlink(os.path.join('/opt/events', filename))


def update_gauges_from_gcal(*_unused_arguments_needed_for_scheduler):
    """Fetch calendar events in the lookback window and cache them as JSON."""
    logging.info("Updating gcal metrics ")

    now_dt = datetime.datetime.utcnow()
    time_max = now_dt.isoformat() + 'Z'
    time_min = (now_dt - timedelta(days=args.lookbackDays)).isoformat() + 'Z'

    # googleapiclient Resource is built dynamically.
    events_result = GCAL_CLIENT.events().list(  # pylint: disable=no-member
        calendarId='primary',
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy='startTime',
    ).execute()

    events = events_result.get('items', [])

    clear_events_cache()

    if not events:
        logging.info("No events found")
        return

    for event in events:
        try:
            with open(
                '/opt/events/' + event['id'],
                'w',
                encoding='utf-8',
            ) as output:
                json.dump(event, output)

        except (OSError, TypeError, KeyError, ValueError) as err:
            # eg, if this script is started with a label that exists, that is then deleted
            # after startup, 404 exceptions are thrown.
            #
            # Occsionally, the gcal API will throw garbage, too. Hence the try/catch.
            logging.error("Error: %s", err)


def get_gcal_client():
    """Build an authorized Google Calendar API client."""
    credentials = get_credentials()
    http_client = credentials.authorize(httplib2.Http())
    return discovery.build('calendar', 'v3', http=http_client)


def infinite_update_loop():
    """Periodically refresh event cache and rebuild Prometheus gauges."""
    while True:
        try:
            update_gauges_from_gcal()
            read_events_files()
        except Exception:  # pylint: disable=broad-exception-caught
            logging.exception("Update loop failed")

        sleep(args.updateDelaySeconds)


def get_minutes(event):
    """Return event duration in whole minutes."""
    start = parsedate(event['start']['dateTime'])
    end = parsedate(event['end']['dateTime'])

    return abs((end - start).seconds // 60)


def is_external(event):
    """Return True if the organizer or any attendee is outside internalDomain."""
    if "attendees" in event:
        attendees = event['attendees']
    else:
        attendees = []

    return (
        has_external_addresses([event['organizer']])
        or has_external_addresses(attendees)
    )


def has_external_addresses(addresses):
    """Return True if any address email is outside the configured internal domain."""
    for addr in addresses:
        if args.internalDomain not in addr['email']:
            return True

    return False


def analyze_message(event):
    """Update gauges for a single timed multi-attendee calendar event."""
    logging.info("Analizing id:%s summary:%s", event['id'], event['summary'])

    if "dateTime" not in event['start']:
        logging.info("All day event, ignoring")
        return

    if "attendees" not in event or len(event['attendees']) < 2:
        logging.info("%s Event with 0 or 1 attendees, ignoring", event['summary'])
        return

    event['minutes'] = get_minutes(event)
    event['isExternal'] = is_external(event)

    start = parsedate(event['start']['dateTime'])

    datestamp = str(start.year) + "-" + str(start.month) + "-" + str(start.day)

    logging.info(
        "%s Date/time calculations are date:%s mins:%d",
        event['id'],
        datestamp,
        event['minutes'],
    )

    if args.debugEvents:
        for key in event:
            logging.debug("%s : %s", key, event[key])

    gauge_mins_available.labels(date=datestamp).set(480)
    gauge_mins_used.labels(date=datestamp).inc(event['minutes'])
    gauge_count.labels(date=datestamp).inc(1)

    if event['isExternal']:
        gauge_mins_external.labels(date=datestamp).inc(event['minutes'])
        gauge_count_external.labels(date=datestamp).inc(1)
    else:
        gauge_mins_internal.labels(date=datestamp).inc(event['minutes'])
        gauge_count_internal.labels(date=datestamp).inc(1)

    meeting_class = classify_event(
        event,
        args.meeting_class_prefixes,
        args.meeting_class_colors,
    )
    class_labels = {'date': datestamp, 'class': meeting_class}
    gauge_mins_by_class.labels(**class_labels).inc(event['minutes'])
    gauge_count_by_class.labels(**class_labels).inc(1)

    for optionality, response in attendee_buckets(event):
        gauge_attendees.labels(
            date=datestamp,
            optionality=optionality,
            response=response,
        ).inc(1)


def clear_gauge_metrics():
    """Clear label samples so gauges rebuild cleanly each scrape cycle."""
    # prometheus_client has no public API to drop all samples for a gauge.
    for gauge in _ALL_GAUGES:
        gauge._metrics.clear()  # pylint: disable=protected-access


def read_events_files():
    """Rebuild gauges from cached event JSON files."""
    clear_gauge_metrics()

    for filename in os.listdir('/opt/events'):
        with open(
            '/opt/events/' + filename,
            'r',
            encoding='utf-8',
        ) as json_file:
            event = json.load(json_file)

            analyze_message(event)


def start_waitress():
    """Serve the Flask / Prometheus HTTP endpoints."""
    waitress.serve(app, host="0.0.0.0", port=args.promPort)


def set_readiness(value):
    """Set the readiness probe message (empty string means ready)."""
    _readiness[0] = value


@app.route("/readyz")
def readyz():
    """Kubernetes-style readiness endpoint."""
    if _readiness[0] == "":
        return "OK"
    return Response(_readiness[0], status=503)


@app.route("/")
def index():
    """Root health/info page."""
    return "prometheus-gcal-exporter"


def main():
    """Start the metrics server and enter the update loop."""
    global GCAL_CLIENT  # pylint: disable=global-statement

    logging.info("prometheus-gcal-exporter started on port %d", args.promPort)

    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
        '/metrics': make_wsgi_app()
    })

    thread = Thread(target=start_waitress)
    thread.start()

    GCAL_CLIENT = get_gcal_client()

    infinite_update_loop()


if __name__ == '__main__':
    logging.getLogger().setLevel(20)

    parser = configargparse.ArgumentParser(
        default_config_files=[
            get_file_path('prometheus-gcal-exporter.ini'),
            "/etc/prometheus-gcal-exporter/config.ini",
        ],
    )
    parser.add_argument('labels', nargs='*', default=[])
    parser.add_argument(
        '--clientSecretFile',
        default=get_file_path('client_secret.json'),
    )
    parser.add_argument(
        '--credentialsPath',
        default=get_file_path('login_cookie.dat'),
    )
    parser.add_argument("--updateDelaySeconds", type=int, default=1800)
    parser.add_argument('--internalDomain', required=True)
    parser.add_argument("--lookbackDays", type=int, default=14)
    parser.add_argument(
        "--meetingClassPrefix",
        action="append",
        default=[],
        help="Meeting class from title prefix, className=PREFIX (repeatable)",
    )
    parser.add_argument(
        "--meetingClassColor",
        action="append",
        default=[],
        help="Meeting class from Google colorId, className=colorId (repeatable)",
    )
    parser.add_argument("--promPort", type=int, default=8080)
    parser.add_argument("--debugEvents", action='store_true')
    args = parser.parse_args()
    args.meeting_class_prefixes = parse_class_pairs(args.meetingClassPrefix)
    args.meeting_class_colors = build_color_map(args.meetingClassColor)

    try:
        main()
    except KeyboardInterrupt:
        print("\n")  # Terminals often print Ctrl+C too; keep logs readable.
        logging.info("Ctrl+C, bye!")
