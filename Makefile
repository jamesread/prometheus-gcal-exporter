lint:
	pylint-3 gcal-exporter.py

buildah:
	buildah bud -t docker.io/jamesread/prometheus-google-calendar-exporter .

docker:
	docker build -t docker.io/jamesread/prometheus-google-calendar-exporter .
