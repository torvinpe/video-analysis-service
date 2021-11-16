# API

## Upload file for analysis

The response will contain a `file_id` identifier which can later be used to
refer to the uploaded file.

POST `/upload`

Payload: _the file to upload_

Response:

    HTTP 200
    { file_id: 42 }

## Delete file

Should we also have this? There needs to anyway be some kind of clean-up of old
files, e.g., after specific time, if running out of space, or similiar?

DELETE `/upload/file_id` ?


## Start analysis for file

Start analysis for previously uploaded file. In theory we could start many
different types of analyses for the same file. Response will give `analysis_id`,
an identifier for later accessing the analysis results.

POST `/analyse`

Payload (JSON):

    { 
        file_id: 42,
        analysis: 'name_of_analysis'
    }

Response (HTTP 200):

    { file_id: 42, analysis_id: 123 }

## Get analysis

Endpoint to poll if results are ready yet. We respond perhaps with HTTP 202 and
empty content if no result yet (but the result_id was a valid one). When result
is ready we return (HTTP 200) the results JSON. The result can contain links to
files related to the analysis which need to be downloaded separately.

GET `/results/<file_id>/<analysis_id>`

Response (not ready yet):

    HTTP 202

Response (ready, HTTP 200):

    {
        result: [0, 1, 2],
        video_file: /download/foo_bar.mp4
    }

## Get video result

GET `/download/foo_bar.mp4`


# Implementation

First iteration: try to do without actual database. Things are stored in the
filesystem only. Keep in mind concurrency, i.e., should be able to accept
multiple requests at the same time and not break.

## Upload file

- create file id, perhaps SHA1 (like git) of file contents, or filename +
  timestamp
  
- create file to `/data/<file_id>/safename.ext`
  or possibly `/data/<file_id>[:2]/<file_id>[2:]/` like in git

- symlink `/data/<file_id>/the_file` -> safename.ext

- return file_id to user


## Analysis for file

Given `file_id` and analysis name from user (plus arguments for analysis?).

Implement 'dummy_N' analysis that just idles for N seconds.

    { file_id: 42, analysis: 'dummy', args: { 'seconds': 60 } }

- create analysis_id, sha1 of `file_id` + analysis_name args?

- create `/data/<file_id>/analysis/<analysis_id>` folder, containing

  - config.json, user given config?
  - inrun.pid -> still running with pid
  - results.json
  
Get analysis API checks above files based on `file_id` and `analysis_id` given
in request.



