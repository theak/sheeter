# Alpine.  Every requirement has a musllinux wheel on both amd64 and arm64, so nothing
# is built from source here.  That is only true because the Anthropic SDK is not one of
# them: it pulls in jiter, which publishes no musl wheel at all, and pip does not fail
# on that, it quietly resolves back to an SDK too old to send the strict tool schemas
# in sheeter/verify.py.  sheeter/claude.py is a few dozen lines of urllib instead.
FROM python:3.13-alpine
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ADD requirements.txt /app/
# --only-binary keeps that honest: if a musl wheel ever goes missing the build fails
# here instead of quietly compiling, or quietly resolving back to an older release.
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
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
