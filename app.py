import db
import hashlib
import io
import os
import time

from celery import Celery
from flask import Flask, request, make_response, jsonify, send_file
from werkzeug.utils import secure_filename


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
    DATA_FOLDER='/media/data/code/video-analysis-service/data',
    result_backend='redis://localhost:6379',
    CELERY_broker_url='redis://localhost:6379'
)
celery = make_celery(app)

db.init_app(app)

# Database
#
# Table files
# - filename, file path + name starting from DATA_FOLDER
# - sha1, hash of file contents, should be unique


def calculate_hash(fp):
    hash = hashlib.sha1()
    while chunk := fp.read(8192):
        hash.update(chunk)
    return hash.hexdigest()


def data_path(db_fname):
    return os.path.join(app.config['DATA_FOLDER'], db_fname)


def handle_upload(request):
    # Check if we are receiving a valid file upload (multipart/form-data)
    if 'file' not in request.files:
        return make_response(("No file given\n", 400))
    fp = request.files['file']
    if fp.filename == '':
        return make_response(("Empty file\n", 400))

    # Calculate SHA1 hash of file
    content_type = fp.content_type
    fname = secure_filename(fp.filename)
    hash_digest = calculate_hash(fp)

    # Open database
    con = db.get_db()
    cur = con.cursor()

    # Check if SHA1 already present in database
    file_exists = False
    cur.execute("SELECT filename FROM files WHERE sha1=?", (hash_digest,))
    found = cur.fetchone()
    if found:
        file_exists = os.path.exists(data_path(found['filename']))

    # If no SHA1 in db, or its corresponding file is missing...
    if not file_exists:
        subdir = hash_digest[:8]  # directory, inside DATA_FOLDER
        fname = os.path.join(subdir, fname)  # file inside DATA_FOLDER

        # Create dir if doesn't exist
        os.makedirs(os.path.dirname(data_path(fname)), exist_ok=True)

        # Save file
        fp.seek(0)
        fp.save(data_path(fname))

        cur.execute("REPLACE INTO files (filename, sha1, content_type) VALUES (?, ?, ?)",
                    (fname, hash_digest, content_type))
        con.commit()
    else:
        print('File {} with sha1 {} already found in database'.format(
            fname, hash_digest))

    cur.close()
    return jsonify({'file_id': hash_digest}), 202


def get_fileinfo(file_id):
    con = db.get_db()
    cur = con.cursor()
    fields = ['sha1', 'filename', 'content_type']
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
    if len(result) == 0:
        return make_response(("No file with that id!\n", 400))
    if len(result) > 1:
        return make_response(("Ambiguous id, matched more than one file!\n", 400))

    fname = result[0]['filename']
    with open(data_path(fname), 'rb') as fp:
        print('Returning file', fname)
        bindata = fp.read()
        return send_file(io.BytesIO(bindata),
                         attachment_filename=os.path.basename(fname),
                         mimetype=result[0]['content_type'])

    return make_response(("Unable to read data\n", 500))


@celery.task
def analyse_video(fname, sha1):
    print('Starting FAKE analysis of video {}...'.format(fname))
    time.sleep(30)

    # TODO: save as new file to table with own hash?
    # or does DeepLabCut assume some files to exist in specific dir?

    # hash_digest = calculate_hash(open(csv_fname))

    # # Open database
    # con = db.get_db()
    # cur = con.cursor()

    print('Analysis done!')
    return {'result': 42,
            'file_id': sha1,
            'csv_file': 'foo'}


@celery.task
def analyse_sleep(fname, sleep_time):
    print('Sleeping for {} seconds...'.format(sleep_time))
    time.sleep(sleep_time)

    # Create CSV file
    import csv
    ldir = os.path.dirname(fname)
    csv_fname = os.path.join(ldir, 'output.csv')
    with open(data_path(csv_fname), 'w') as fp:
        w = csv.writer(fp)
        w.writerow(['sleep', sleep_time])

    # Calculate SHA1 from CSV file
    hash_digest = calculate_hash(open(data_path(csv_fname), 'rb'))

    # Open database
    con = db.get_db()
    cur = con.cursor()

    # Add database entry for CSV file
    cur.execute("REPLACE INTO files (filename, sha1, content_type) VALUES (?, ?, ?)",
                (csv_fname, hash_digest, 'text/csv'))
    con.commit()

    return {'sleep_time': sleep_time, 'csv_file': hash_digest}


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
    analysis = request.form.get('analysis')

    if analysis is None:
        return make_response(("No analysis task given\n", 400))
    analysis = analysis.lower()

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
    short_fname = found['filename']

    if not os.path.exists(data_path(short_fname)):
        return make_response(("File no longer exists in server\n", 400))

    if analysis == 'sleep':
        sleep_time = request.form.get('time')
        if sleep_time is None:
            return make_response(("No time argument given\n", 400))
        task = analyse_sleep.apply_async(args=(short_fname, int(sleep_time)))
    elif analysis == 'video':
        task = analyse_video.apply_async(args=(short_fname, hash_digest))
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
