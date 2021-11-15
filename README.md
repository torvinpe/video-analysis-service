# API

## Upload file for analysis

The response will contain a `file_id` identifier which can later be used to refer to the uploaded file.

POST `/upload`

Payload: _the file to upload_

Response:

    HTTP 200
    { file_id: 42 }

## Delete file

Should we also have this? There needs to anyway be some kind of clean-up of old files, e.g., after specific time, if running out of space, or similiar?


## Start analysis for file

Start analysis for previously uploaded file. In theory we could start many different types of analyses for the same file. Response will give `result_id`, an identifier for later accessing the analysis results.

POST `/analyse`

Payload (JSON):

    { 
        file_id: 42,
        analysis: 'name_of_analysis'
    }

Response (HTTP 200):

    { result_id: 123 }

## Get analysis

Endpoint to poll if results are ready yet. We respond perhaps with HTTP 202 and empty content if no result yet (but the result_id was a valid one). When result is ready we return (HTTP 200) the results JSON. The result can contain links to files related to the analysis which need to be downloaded separately.

GET `/results/<result_id>`

Response (not ready yet):

    HTTP 202

Response (ready, HTTP 200):

    {
        result: [0, 1, 2],
        video_file: /download/foo_bar.mp4
    }

## Get video result

GET `/download/foo_bar.mp4`
