IMAGE_NAME ?= network-cost-ui
AWS_REGION ?= us-west-2
ATHENA_DB  ?= default
S3_BUCKET  ?= $(error S3_BUCKET is required — set it via make run S3_BUCKET=your-bucket-name)
PORT       ?= 8501

.PHONY: build run stop clean

build:
	docker build -t $(IMAGE_NAME) .

run: build
	docker run -d --name $(IMAGE_NAME) \
		-p $(PORT):8501 \
		-e AWS_REGION=$(AWS_REGION) \
		-e ATHENA_DATABASE=$(ATHENA_DB) \
		-e S3_BUCKET=$(S3_BUCKET) \
		-v ~/.aws:/root/.aws:ro \
		$(IMAGE_NAME)
	@echo "Dashboard running at http://localhost:$(PORT)"

stop:
	-docker stop $(IMAGE_NAME)
	-docker rm $(IMAGE_NAME)

clean: stop
	-docker rmi $(IMAGE_NAME)
