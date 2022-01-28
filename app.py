import db
import hashlib
import io
import os
import time
import yaml
import sys

from celery import Celery
from flask import Flask, request, make_response, jsonify, send_file
from werkzeug.utils import secure_filename

CONFIG_FILE = "config.yaml"


# Celery support with help from these links:
# https://flask.palletsprojects.com/en/2.0.x/patterns/celery/
# https://github.com/miguelgrinberg/flask-celery-example/blob/master/app.py


def make_celery(app):
    celery = Celery(
        app.import_name,
        backend=app.config['result_backend'],
        broker=app.config['CELERY_broker_url'],
        task_track_started=True
    )
    celery.conf.update(app.config)

    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery


app = Flask(__name__)

try:
    with open(CONFIG_FILE) as fp:
        yaml_config = yaml.safe_load(fp)
        app.config.update(yaml_config)
except FileNotFoundError:
    print('\nERROR: config file "{}" not found! You need to create a config file.\n'
          'See the example in "config.yaml.example".\n'.format(CONFIG_FILE))
    sys.exit(1)

celery = make_celery(app)
db.init_app(app)


def calculate_hash(fp):
    hash = hashlib.sha1()
    while chunk := fp.read(8192):
        hash.update(chunk)
    result = hash.hexdigest()
    assert len(result) == 40  # sanity check
    return result


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
    cur.execute("SELECT filename FROM files WHERE file_id=?", (hash_digest,))
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

        cur.execute("REPLACE INTO files (filename, file_id, content_type) VALUES (?, ?, ?)",
                    (fname, hash_digest, content_type))
        con.commit()
    else:
        print('File {} with file_id {} already found in database'.format(
            fname, hash_digest))

    cur.close()
    return jsonify({'file_id': hash_digest}), 202


def get_fileinfo(file_id):
    con = db.get_db()
    cur = con.cursor()

    fields = ['file_id', 'filename', 'content_type', 'created']
    sql_query = "SELECT {} FROM files".format(','.join(fields))
    if file_id is None:
        cur.execute(sql_query)
    else:
        cur.execute(sql_query + " WHERE file_id LIKE ? ", (file_id + '%',))
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


@app.route('/file/<file_id>', methods=['GET', 'DELETE'])
def get_single_file(file_id):
    result = get_fileinfo(file_id)
    if len(result) == 0:
        return make_response(("No file with that id!\n", 400))
    if len(result) > 1:
        return make_response(("Ambiguous id, matched more than one file!\n", 400))

    fname = result[0]['filename']
    real_fname = data_path(fname)

    if request.method == 'DELETE':
        sha1 = result[0]['file_id']
        assert len(sha1) == 40  # sanity check

        con = db.get_db()
        cur = con.cursor()
        cur.execute("DELETE FROM files WHERE file_id = ?", (result[0]['file_id'],))
        con.commit()
        cur.close()

        try:
            os.remove(real_fname)
        except FileNotFoundError:
            print('WARNING: file to be removed not found: {}'.format(real_fname))

        return make_response(("Successfully deleted file {}\n".format(fname), 200))
    else:
        with open(real_fname, 'rb') as fp:
            print('Returning file', fname)
            bindata = fp.read()
            return send_file(io.BytesIO(bindata),
                             attachment_filename=os.path.basename(fname),
                             mimetype=result[0]['content_type'])

    return make_response(("Unable to read data\n", 500))


def add_results_file(fname, content_type):
    # Calculate SHA1 from file
    hash_digest = calculate_hash(open(data_path(fname), 'rb'))

    # Open database
    con = db.get_db()
    cur = con.cursor()

    # Add database entry for CSV file
    cur.execute("REPLACE INTO files (filename, file_id, content_type) "
                "VALUES (?, ?, ?)", (fname, hash_digest, content_type))
    con.commit()
    cur.close()

    return hash_digest


@celery.task
def analyse_video(fname):
    import deeplabcut

    print('Starting analysis of video {}...'.format(fname))

    res_id = deeplabcut.analyze_videos(app.config['DLC_CONFIG'],
                                       [data_path(fname)],
                                       save_as_csv=True)
    csv_fname = os.path.splitext(fname)[0] + res_id + '.csv'
    assert os.path.exists(data_path(csv_fname))

    hash_digest = add_results_file(csv_fname, "text/csv")

    return {'csv_file': hash_digest}


@celery.task
def create_labeled_video(fname):
    import deeplabcut

    print('Creating labeled video for {}...'.format(fname))

    from deeplabcut.utils import auxiliaryfunctions

    cfg_fname = app.config['DLC_CONFIG']
    deeplabcut.create_labeled_video(cfg_fname,
                                    [data_path(fname)],
                                    videotype='.mp4',
                                    draw_skeleton=True)

    cfg = auxiliaryfunctions.read_config(cfg_fname)
    res_id, _ = auxiliaryfunctions.GetScorerName(
        cfg, shuffle=1, trainFraction=cfg["TrainingFraction"][0])

    video_fname = os.path.splitext(fname)[0] + res_id + '_labeled.mp4'
    assert os.path.exists(data_path(video_fname))

    hash_digest = add_results_file(video_fname, "video/mp4")

    return {'labeled_video': hash_digest}


# TODO
#    >>deeplabcut.triangulate(config_path3d, '/fullpath/videofolder', save_as_csv=True)
#    >>deeplabcut.create_labeled_video_3d(config_path3d, ['/fullpath/videofolder']

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
    hash_digest = add_results_file(csv_fname, "text/csv")

    return {'sleep_time': sleep_time, 'csv_file': hash_digest}


def get_analysis_list():
    con = db.get_db()
    cur = con.cursor()

    fields = ['task_id', 'file_id', 'analysis_name', 'state', 'created']
    cur.execute('SELECT id, {} FROM analyses'.format(', '.join(fields)))

    return [{f: row[f] for f in fields} for row in cur.fetchall()]


@app.route('/analysis', methods=['GET', 'POST'])
def start_analysis():
    if request.method == 'GET':
        return jsonify(get_analysis_list()), 200

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
    cur.execute('SELECT filename, file_id FROM files WHERE file_id LIKE ?', (file_id+'%',))
    found = cur.fetchone()
    if not found:
        return make_response(("Given file_id not found\n", 400))

    fname = found['filename']

    if not os.path.exists(data_path(fname)):
        return make_response(("File no longer exists in server\n", 400))

    if analysis == 'sleep':
        sleep_time = request.form.get('time')
        if sleep_time is None:
            return make_response(("No time argument given\n", 400))
        task = analyse_sleep.apply_async(args=(fname, int(sleep_time)))
    elif analysis == 'video':
        task = analyse_video.apply_async(args=(fname,))
    elif analysis == 'label':
        task = create_labeled_video.apply_async(args=(fname,))
    else:
        return make_response(("Unknown analysis task '{}'\n".format(analysis)),
                             400)

    task_id = task.id
    cur.execute("INSERT INTO analyses (task_id, file_id, analysis_name, state) "
                "VALUES (?, ?, ?, ?)", (task_id, found['file_id'], analysis, "PENDING"))
    con.commit()

    return jsonify({'task_id': task_id}), 202


@app.route('/analysis/<task_id>')
def get_analysis(task_id):
    con = db.get_db()
    cur = con.cursor()

    cur.execute('SELECT id, task_id, state FROM analyses WHERE task_id LIKE ?', (task_id+'%',))
    found = cur.fetchone()
    if not found:
        return make_response(("Given task_id not found\n", 400))

    db_id = found['id']
    task_id = found['task_id']

    task = analyse_video.AsyncResult(task_id)
    state = task.state

    if state != found['state']:
        cur.execute('UPDATE analyses SET state=? WHERE id=?', (state, db_id))
        con.commit()

    state_json = {'state': task.state}
    if task.state in ('PENDING', 'STARTED'):
        return jsonify(state_json), 202
    elif task.state == 'FAILURE':
        return jsonify({'state': task.state,
                        'exception': repr(task.result),
                        'traceback': task.traceback}), 500
    elif task.state == 'SUCCESS':
        return jsonify(task.info)
    else:
        return make_response(("Unexpected task state: '{}'\n".format(task.state)), 500)
