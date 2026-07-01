web: uv run gunicorn baxter.wsgi --bind 0.0.0.0:$PORT --workers 2 --access-logfile - --error-logfile -
release: uv run manage.py migrate
