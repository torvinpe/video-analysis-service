# API

## Upload file

POST `/upload`

Response:

    HTTP 200
    { file_id: 42 }

## Start analysis for file

POST `/analyse`

    { 
        file_id: 42,
        analysis: 'name_of_analysis'
    }

Response:

    HTTP 200
    { result_id: 123 }

## Get analysis

GET /results/<result_id>

Response (not ready yet):

    HTTP 202

Response (ready):

    HTTP 200
    {
        result: [0, 1, 2],
       video_file: /download/foo_bar.mp4
    }

## Get video result

GET `/download/foo_bar.mp4`
