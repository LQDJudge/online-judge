FROM python:3.12

WORKDIR /app

COPY online-judge /app

RUN pip install -r requirements.txt
RUN pip install mysqlclient
RUN pip install setuptools
RUN pip install django-storages[s3]

EXPOSE 9998 9999

CMD ["python3", "manage.py", "runbridged"]
