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


def calculate_hash(fp):
    hash = hashlib.sha1()
    while chunk := fp.read(8192):
        hash.update(chunk)
    return hash.hexdigest()


def local_path(hash_digest, fname):
    short_hash = hash_digest[:8]
    local_dir = os.path.join(app.config['DATA_FOLDER'], short_hash)
    return os.path.join(local_dir, fname)


def handle_upload(request):
    # Check if we are receiving a valid file upload (multipart/form-data)
    if 'file' not in request.files:
        return make_response(("No file given\n", 400))
    fp = request.files['file']
    if fp.filename == '':
        return make_response(("Empty file\n", 400))

    # Calculate SHA1 hash of file
    fname = secure_filename(fp.filename)
    hash_digest = calculate_hash(fp)

    # Open database
    con = db.get_db()
    cur = con.cursor()

    # Check if SHA1 present already in database
    file_exists = False
    cur.execute("SELECT filename FROM files WHERE sha1=?", (hash_digest,))
    found = cur.fetchone()
    if found:
        local_fname = local_path(hash_digest, found['filename'])
        file_exists = os.path.exists(local_fname)

    # If no SHA1 in db, or its corresponding file is missing...
    if not file_exists:
        local_fname = local_path(hash_digest, fname)
        os.makedirs(os.path.dirname(local_fname), exist_ok=True)
        fp.save(local_fname)

        cur.execute("REPLACE INTO files (filename, sha1) VALUES (?, ?)",
                    (fname, hash_digest))
        con.commit()
    else:
        print('File {} with sha1 {} already found in database'.format(
            fname, hash_digest))

    cur.close()
    return jsonify({'file_id': hash_digest}), 202


def get_fileinfo(file_id):
    con = db.get_db()
    cur = con.cursor()
    fields = ['sha1', 'filename']
    sql_query = "SELECT {} FROM files".format(','.join(fields))
    if file_id is None:
        cur.execute(sql_query)
    else:
        cur.execute(sql_query + " WHERE sha1 LIKE ? ", (file_id + '%',))
    result = [{f: row[f] for f in fields}
              for row in cur.fetchall()]
    cur.close()
    return result


@app.route('/file', methods=['GET', 'POST'])
def get_all_files():
    if request.method == 'POST':
        return handle_upload(request)
    else:
        return jsonify(get_fileinfo(None)), 200


@app.route('/file/<file_id>')
def get_single_file(file_id):
    result = get_fileinfo(file_id)
    if len(result) != 1:
        print('WARNING: GET {} produced {} rows instead of one!'.format(
            file_id, len(result)))
    return jsonify(result[0]), 200


@celery.task
def analyse_video(fname, sha1):
    print('Starting FAKE analysis of video {}...'.format(fname))
    time.sleep(30)

    import csv
    ldir = os.path.dirname(fname)
    csv_fname = os.path.join(ldir, 'output.csv')
    with open(csv_fname, 'w') as fp:
        w = csv.writer(fp)
        w.writerow(['x', 'y', 'value'])
        w.writerow([1.0, 2.0, 33.0])
        w.writerow([3.0, 4.0, 42.0])

    # TODO: save as new file to table with own hash?
    # or does DeepLabCut assume some files to exist in specific dir?
        
    # hash_digest = calculate_hash(open(csv_fname))

    # # Open database
    # con = db.get_db()
    # cur = con.cursor()
    
        
    print('Analysis done!')
    return {'result': 42,
            'file_id': sha1,
            'csv_file': }


@celery.task
def analyse_sleep(sleep_time):
    print('Sleeping for {} seconds...'.format(sleep_time))
    time.sleep(sleep_time)
    return {'sleep_time': sleep_time}


@app.route('/analysis', methods=['POST'])
def start_analysis():
    # if request.method == 'GET':
    #     i = celery.control.inspect()
    #     print(i.scheduled())
    #     print(i.active())
    #     print(i.reserved())
    #     return make_response(("OK", 200))

    # Check arguments
    if 'file_id' not in request.form:
        return make_response(("Not file_id given\n", 400))
    file_id = request.form['file_id']
    analysis = request.form.get('analysis').lower()

    if analysis is None:
        return make_response(("No analysis task given\n", 400))

    # Open database
    con = db.get_db()
    cur = con.cursor()

    # Find file_id in database
    file_exists = False
    cur.execute('SELECT filename, sha1 FROM files WHERE sha1 LIKE ?', (file_id+'%',))
    found = cur.fetchone()
    if not found:
        return make_response(("Given file_id not found\n", 400))
    hash_digest = found['sha1']
    local_fname = local_path(hash_digest, found['filename'])

    if not os.path.exists(local_fname):
        return make_response(("File no longer exists in server\n", 400))

    if analysis == 'sleep':
        sleep_time = request.form.get('time')
        if sleep_time is None:
            return make_response(("No time argument given\n", 400))
        task = analyse_sleep.apply_async(args=(int(sleep_time), ))
    elif analysis == 'video':
        task = analyse_video.apply_async(args=(local_fname, hash_digest))
    else:
        return make_response(("Unknown analysis task '{}'\n".format(analysis)),
                             400)

    return jsonify({'task_id': task.id}), 202


@app.route('/analysis/<analysis_id>')
def get_analysis(analysis_id):
    task = analyse_video.AsyncResult(analysis_id)
    state = {'state': task.state}
    if task.state == 'PENDING':
        return jsonify(state), 202
    elif task.state != 'FAILURE':
        return jsonify(task.info)
    else:
        return jsonify(state), 500
