# Debian rather than Alpine.  Every direct requirement has a musllinux wheel, but jiter,
# which anthropic pulls in, publishes none at all.  On Alpine pip does not fail, it
# quietly resolves back to an anthropic old enough that it cannot send the strict tool
# schemas in sheeter/verify.py.
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ADD requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
COPY sheeter/ /app/sheeter/
COPY static/ /app/static/
COPY templates/ /app/templates/
ADD app.py /app/
# Analyses live on a volume so a container restart keeps the user's history.
ENV SHEETER_DATA_DIR=/data
VOLUME /data
ENV PORT=5000
EXPOSE 5000
# sh so PORT expands, exec so waitress is PID 1 and gets the stop signal itself.
CMD ["sh", "-c", "exec waitress-serve --host 0.0.0.0 --port ${PORT:-5000} app:app"]
