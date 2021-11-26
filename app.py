import hashlib
import os
import time
from celery import Celery
from flask import Flask, request, make_response, jsonify
from werkzeug.utils import secure_filename
import db

# Celery support with help from these links:
# https://flask.palletsprojects.com/en/2.0.x/patterns/celery/
# https://github.com/miguelgrinberg/flask-celery-example/blob/master/app.py


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
    DATABASE='db.sqlite3',
    DATA_FOLDER='/home/msjoberg/code/video-analysis-service/data',
    result_backend='redis://localhost:6379',
    CELERY_broker_url='redis://localhost:6379'
)
celery = make_celery(app)

db.init_app(app)


@celery.task
def analyse_video(fname):
    print('Starting analysis...')
    time.sleep(30)
    print('Analysis done!')
    return {'result': 42, 'filename': fname}


def calculate_hash(fp):
    hash = hashlib.sha1()
    while chunk := fp.read(8192):
        hash.update(chunk)
    return hash.hexdigest()


@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return make_response(("No file given", 400))
    fp = request.files['file']
    if fp.filename == '':
        return make_response(("Empty file", 400))

    fname = secure_filename(fp.filename)
    hash_digest = calculate_hash(fp)

    # TODO check if hash already exists in database
    
    local_dir = os.path.join(app.config['DATA_FOLDER'], hash_digest[:8])
    local_fname = os.path.join(local_dir, fname)
    os.mkdir(local_dir)
    fp.save(local_fname)

    cur = db.get_db().cursor()
    cur.execute("INSERT INTO files (filename, sha1) VALUES (?, ?)",
                (local_fname, hash_digest))
    cur.close()

    # TODO why not actually storing anything to database?
    
    # task = analyse_video.apply_async(args=(local_fname,))

    # return jsonify({'task_id': task.id}), 202
    return jsonify({'file_id': hash_digest}), 202


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
