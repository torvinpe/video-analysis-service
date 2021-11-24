import os
import time
from celery import Celery
from flask import Flask, request, make_response, jsonify
from werkzeug.utils import secure_filename


def make_celery(app):
    celery = Celery(
        app.import_name,
        backend=app.config['result_backend'],
        broker=app.config['CELERY_broker_url']
    )
    celery.conf.update(app.config)

    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery


app = Flask(__name__)
app.config.update(
    DATA_FOLDER='/home/msjoberg/code/video-analysis-service/data',
    result_backend='redis://localhost:6379',
    CELERY_broker_url='redis://localhost:6379'
)
celery = make_celery(app)


@celery.task
def analyse_video(fname):
    print('Starting analysis...')
    time.sleep(30)
    print('Analysis done!')
    return {'result': 42, 'filename': fname}


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return make_response(("No file given", 400))
    fp = request.files['file']
    if fp.filename == '':
        return make_response(("Empty file", 400))

    fname = secure_filename(fp.filename)
    local_fname = os.path.join(app.config['DATA_FOLDER'], fname)
    fp.save(local_fname)

    task = analyse_video.apply_async(args=(local_fname,))

    return jsonify({'task_id': task.id}), 202


@app.route('/results/<analysis_id>')
def get_analysis(analysis_id):
    task = analyse_video.AsyncResult(analysis_id)
    state = {'state': task.state}
    if task.state == 'PENDING':
        return jsonify(state), 202
    elif task.state != 'FAILURE':
        return jsonify({'result': task.info['result']})
    else:
        return jsonify(state), 500
