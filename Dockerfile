FROM fedora

RUN mkdir /opt/events/

RUN dnf -y update && \
	dnf -y install python3-configargparse python3-httplib2 python3-google-api-client.noarch python3-prometheus_client.noarch python3-dateutil python3-waitress python3-flask python3-oauth2client && \
	dnf clean all

COPY gcal-exporter.py /usr/local/sbin/gcal-exporter

ENTRYPOINT [ "/usr/local/sbin/gcal-exporter" ] 
