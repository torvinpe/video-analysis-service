# Installation

```bash
sudo yum install redis
pip install flask celery[redis]
```

# Usage

Important: `--concurrency=1` flag to celery so that it runs only one job at a time.

```bash
sudo systemctl start redis
celery -A app.celery worker --concurrency=1
flask run  # in separate terminal
```

For development, `export FLASK_ENV=development` might also be handy. Before the
first run you also need to initialize the sqlite3 database with `flask init-db`.

Testing upload (example):

```bash
curl -F "file=@hello.txt" http://127.0.0.1:5000/file
```

Start analysis (example):

```bash
curl -d file_id=7b4758d4 -d analysis=sleep -d time=10 http://127.0.0.1:5000/analysis
```

Check results (example):

```bash
curl http://127.0.0.1:5000/analysis/32355caf-f322-4b05-b1c2-878d1e9be272
```


# API

## Upload file for analysis

The response will contain a `file_id` identifier (generated by server) which can
later be used to refer to the uploaded file.

POST `/file`

Payload: multipart/form-data format, file as `file` parameter

Response (example):

    HTTP 200
    { file_id: 123 }
    
## List uploaded files

GET `/file`

## Download specific file

GET `/file/<file_id>`

The given file id can be the first characters of the SHA1, returned if there is
a single unambiguous match.

## Delete file

Should we also have this? There needs to anyway be some kind of clean-up of old
files, e.g., after specific time, if running out of space, or similiar?

DELETE `/file/<file_id>` ?


## Start analysis for file

Start analysis for previously uploaded file. In theory we could start many
different types of analyses for the same file. Response will give `analysis_id`,
an identifier for later accessing the analysis results.

POST `/analysis`

Parameters (multipart/form-data format):

- `file_id` file id as received when uploading file
- `analysis` name of analysis

The rest of the parameters can be used as arguments for the analysis.

Response (HTTP 200):

    { analysis_id: 42 }

## Get analysis

Endpoint to poll if results are ready yet. We respond perhaps with HTTP 202 and
empty content if no result yet (but the analysis_id was a valid one). When result
is ready we return (HTTP 200) the results as JSON. The result can contain links to
files related to the analysis which need to be downloaded separately.

GET `/analysis/<analysis_id>`

Response (not ready yet):

    HTTP 202

Response (ready, HTTP 200):

    {
        result: [0.2, 0.99, -4.0],
        video_file: afa468f55c2d7a9ba62da5a1b33caa55d393c730,
    }

Files are given as hashes, which you can fetch with `GET /file/<sha1_hash>`
