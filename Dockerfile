# rack-panel: Python standard library only, run as nobody.
# openssh-client: the restart module restarts containers through a forced-command SSH key.
#   docker run -d --network host -v $PWD/config:/etc/rack-panel:ro ghcr.io/jbellirigenty/rack-panel:1
FROM alpine:3.22
RUN apk add --no-cache python3 openssh-client
COPY rack-panel.py page.html /app/
ENV PYTHONDONTWRITEBYTECODE=1
USER nobody
ENTRYPOINT ["python3", "/app/rack-panel.py", "-c"]
CMD ["/etc/rack-panel/config.json"]
