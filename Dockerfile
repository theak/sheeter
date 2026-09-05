FROM python:3.13-alpine
WORKDIR /app
ADD requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY sheeter/ /app/sheeter/
COPY static/ /app/static/
COPY templates/ /app/templates/
ADD app.py /app/
# Analyses live on a volume so a container restart keeps the user's history.
ENV SHEETER_DATA_DIR=/data
VOLUME /data
EXPOSE 5000
CMD ["waitress-serve", "--host", "0.0.0.0", "--port", "5000", "app:app"]
