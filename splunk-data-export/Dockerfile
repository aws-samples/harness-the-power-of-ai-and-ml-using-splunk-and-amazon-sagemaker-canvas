FROM public.ecr.aws/lambda/python:3.11
COPY ./splunk-data-export/ ./
RUN python3.11 -m pip install -r requirements.txt
CMD ["app.lambda_handler"]