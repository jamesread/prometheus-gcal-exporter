lint:
	pylint-3 gcal-exporter.py gcal_classify.py

test:
	python3 -m unittest test_classify.py -v

buildah:
	buildah bud -t docker.io/jamesread/prometheus-google-calendar-exporter .

docker:
	docker build -t docker.io/jamesread/prometheus-google-calendar-exporter .
