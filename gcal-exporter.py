#!/usr/bin/python3
"""
Checks Google Calendar and exports metrics via prometheus.
"""

import os
import json
import sys
from time import time, sleep
import logging
import datetime
from datetime import timedelta
from dateutil.parser import parse as parsedate

import httplib2
import configargparse

from prometheus_client import start_http_server, Gauge

from googleapiclient import discovery
from oauth2client import client
from oauth2client.file import Storage

def get_file_path(filename):
    config_dir = os.path.join(os.path.expanduser("~"), ".prometheus-gcal-exporter")

    if not os.path.exists(config_dir):
        os.mkdir(config_dir)

    return os.path.join(config_dir, filename)

def get_credentials():
    """Gets valid user credentials from storage.

    If nothing has been stored, or if the stored credentials are invalid,
    the OAuth2 flow is completed to obtain the new credentials.
    """

    if not os.path.exists(args.clientSecretFile):
        logging.fatal("Client secrets file does not exist: %s . You probably need to download this from the Google API console.", args.clientSecretFile)
        sys.exit()

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
    flow.redirect_uri = client.OOB_CALLBACK_URN
    authorize_url = flow.step1_get_authorize_url()
    
    logging.info("Go and authorize at: %s", authorize_url)

    if sys.stdout.isatty():
        code = input('Enter code:').strip()
    else:
        logging.info("Waiting for code at " + get_file_path('auth_code'))

        while True:
            try:
                if os.path.exists(get_file_path('auth_code')):
                    with open(get_file_path('auth_code'), 'r') as auth_code_file:
                        code = auth_code_file.read()
                        break
                        
            except Exception as e:
                logging.critical(e)

            sleep(10);
    try:
        credential = flow.step2_exchange(code, http=None)
    except client.FlowExchangeError as e:
        logging.fatal("Auth failure: %s", str(e))
        sys.exit(1)

    store.put(credential)
    credential.set_store(store)

    return credential

gauge_collection = {}
gauge_event = Gauge('gcal_event', 'desc', ['gcal_event_name'])

def update_gauges_from_gcal(*unused_arguments_needed_for_scheduler):
    logging.info("Updating gcal metrics ")

    now = datetime.datetime.utcnow().isoformat() + 'Z'
    then = (datetime.datetime.utcnow() + timedelta(days = 7)).isoformat() + 'Z'

    events_result = GCAL_CLIENT.events().list(calendarId = 'primary', timeMin = now, timeMax = then, singleEvents = True, orderBy = 'startTime').execute()

    events = events_result.get('items', [])

    if not events:
        logging.info("No events found")
        return
    
    for event in events:
        try: 
            for key in event.keys():
                output = open('/opt/events/' + event['id'], 'w')

                json.dump(event, output) 

                output.close();

        except Exception as e:
            # eg, if this script is started with a label that exists, that is then deleted
            # after startup, 404 exceptions are thrown.
            #
            # Occsionally, the gcal API will throw garbage, too. Hence the try/catch.
            logging.error("Error: %s", e)

def get_gcal_client():
    credentials = get_credentials()
    http_client = credentials.authorize(httplib2.Http())
    return discovery.build('calendar', 'v3', http=http_client)

def infinate_update_loop():
    while True:
        try: 
            update_gauges_from_gcal()
            readEventsFiles()
        except Exception as e:
            logging.exception(e)

        sleep(args.updateDelaySeconds)


def getMinutes(event):
    start = parsedate(event['start']['dateTime']);
    end = parsedate(event['end']['dateTime']);

    return abs((end - start).seconds // 60)

def isExternal(event):
    if "attendees" in event:
        attendees = event['attendees']
    else:
        attendees = []

    return hasExternalAddresses([event['organizer']]) or hasExternalAddresses(attendees)

def hasExternalAddresses(addresses):
    for addr in addresses: 
        if args.internalDomain not in addr['email']:
            return True

    return False

gauge_mins_external = Gauge('gcal_mins_external', 'Date metrics', ['date'])
gauge_count_external = Gauge('gcal_count_external', 'Date metrics', ['date'])

gauge_mins_internal = Gauge('gcal_mins_internal', 'Date metrics', ['date'])
gauge_count_internal = Gauge('gcal_count_internal', 'Date metrics', ['date'])

gauge_mins_used = Gauge('gcal_mins_used', 'Date metrics', ['date'])
gauge_mins_available = Gauge('gcal_mins_available', 'Date metrics', ['date'])
gauge_count = Gauge('gcal_count', 'Date metrics', ['date'])

def analyizeMessage(event):
    logging.info("Analizing id:%s summary:%s", event['id'], event['summary'])

    if "dateTime" not in event['start']:
        logging.info("All day event, ignoring")
        return

    if "attendees" not in event or len(event['attendees']) < 2:
        logging.info("%s Event with 0 or 1 attendees, ignoring", event['summary'])
        return

    event['minutes'] = getMinutes(event);
    event['isExternal'] = isExternal(event)

    start = parsedate(event['start']['dateTime'])

    datestamp = str(start.year) + "-" + str(start.month) + "-" + str(start.day)

    logging.info("%s Date/time calculations are date:%s mins:%d", event['id'], datestamp, event['minutes']);

    if args.debugEvents:
        for k in event:
            logging.debug("%s : %s", k, event[k])

    gauge_mins_available.labels(date=datestamp).set(480)
    gauge_mins_used.labels(date=datestamp).inc(event['minutes'])
    gauge_count.labels(date=datestamp).inc(1)


    if event['isExternal']:
        gauge_mins_external.labels(date=datestamp).inc(event['minutes'])
        gauge_count_external.labels(date=datestamp).inc(1)
    else:
        gauge_mins_internal.labels(date=datestamp).inc(event['minutes'])
        gauge_count_internal.labels(date=datestamp).inc(1)


def readEventsFiles():
    gauge_mins_external._metrics.clear()
    gauge_count_external._metrics.clear()

    gauge_mins_internal._metrics.clear()
    gauge_count_internal._metrics.clear()

    gauge_mins_used._metrics.clear();
    gauge_mins_available._metrics.clear();
    gauge_count._metrics.clear();

    for f in os.listdir('/opt/events'):
        with open('/opt/events/' + f, 'r') as jsonFile:
            event = json.load(jsonFile)

            analyizeMessage(event)

def main():
    logging.getLogger().setLevel(20)

    global GCAL_CLIENT
    GCAL_CLIENT = get_gcal_client()

    logging.info("prometheus-gcal-exporter started on port %d", args.promPort)
    start_http_server(args.promPort)

    infinate_update_loop()


if __name__ == '__main__':
    global args
    parser = configargparse.ArgumentParser(default_config_files=[get_file_path('prometheus-gcal-exporter.cfg'), "/etc/prometheus-gcal-exporter.cfg"])
    parser.add_argument('labels', nargs='*', default=[])
    parser.add_argument('--clientSecretFile', default=get_file_path('client_secret.json'))
    parser.add_argument('--credentialsPath', default=get_file_path('login_cookie.dat'))
    parser.add_argument("--updateDelaySeconds", type=int, default=300)
    parser.add_argument('--internalDomain', required = True);
    parser.add_argument("--promPort", type=int, default=8080)
    parser.add_argument("--debugEvents", action='store_true')
    args = parser.parse_args()

    try:
        main()
    except KeyboardInterrupt:
        print("\n") # Most terminals print a Ctrl+C message as well. Looks ugly with our log.
        logging.info("Ctrl+C, bye!")
