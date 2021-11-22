# API

## Upload file for analysis

The response will contain a `file_id` identifier (generated by server) which can
later be used to refer to the uploaded file.

POST `/upload`

Payload: _the file to upload_

Response (example):

    HTTP 200
    { file_id: 123 }

## Delete file

Should we also have this? There needs to anyway be some kind of clean-up of old
files, e.g., after specific time, if running out of space, or similiar?

DELETE `/upload/<file_id>` ?


## Start analysis for file

Start analysis for previously uploaded file. In theory we could start many
different types of analyses for the same file. Response will give `analysis_id`,
an identifier for later accessing the analysis results.

POST `/analyse`

Payload (JSON):

    { 
        file_id: 123,
        analysis: 'deeplabcut_2d',
        args: {
            parameter1: 0.1,
            parameter2: 42,
        }
    }

Response (HTTP 200):

    { analysis_id: 42 }

## Get analysis

Endpoint to poll if results are ready yet. We respond perhaps with HTTP 202 and
empty content if no result yet (but the analysis_id was a valid one). When result
is ready we return (HTTP 200) the results as JSON. The result can contain links to
files related to the analysis which need to be downloaded separately.

GET `/results/<analysis_id>`

Response (not ready yet):

    HTTP 202

Response (ready, HTTP 200):

    {
        result: [0.2, 0.99, -4.0],
        video_file: /download/analysis_42_pose.mp4
    }

## Get result files

GET `/download/analysis_42_pose.mp4`


# Implementation

- Database <del>or file system based only</del>? SQLite probably OK for this application

- How to handle analysis jobs? Is a Task Queue framework needed, such as [Celery](https://docs.celeryproject.org/en/stable/)?

## Upload file

- Store file on disk, e.g., based on `<file_id>`

- Check for duplicates with SHA-1 or similar?

- db table, `uploaded_file`:

  ```id | filename | status | sha-1```

- return `id` to user

## Analysis for file

- db table, `analysis`

  ```id | file_id | analysis_name | args | status```

- Implement 'dummy_N' analysis that just idles for N seconds.

    { file_id: 42, analysis: 'dummy', args: { 'seconds': 60 } }

- Return `id` to user

- How to report back completed analysis? Celery?

## Get analysis

Simply check status in database
