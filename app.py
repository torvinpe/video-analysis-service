import db
import hashlib
import io
import os
import time
import yaml
import sys

from celery import Celery
import flask
from flask import Flask, request, make_response, jsonify, send_file
from werkzeug.utils import secure_filename

CONFIG_FILE = "config.yaml"


# Celery support with help from these links:
# https://flask.palletsprojects.com/en/2.0.x/patterns/celery/
# https://github.com/miguelgrinberg/flask-celery-example/blob/master/app.py

# Make celery object for communicating with task queue service
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

# Read config file
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


# Calculate sha1 hash for given file pointer
def calculate_hash(fp):
    hash = hashlib.sha1()
    while chunk := fp.read(8192):
        hash.update(chunk)
    result = hash.hexdigest()
    assert len(result) == 40  # sanity check
    return result


# Translate from short file path to real absolute path on server
def data_path(db_fname):
    return os.path.join(app.config['DATA_FOLDER'], db_fname)


# Handle file upload
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


# Get file info from sqlite database
def get_fileinfo(file_id):
    con = db.get_db()
    cur = con.cursor()

    fields = ['file_id', 'filename', 'content_type', 'created']
    sql_query = "SELECT {} FROM files".format(','.join(fields))
    if file_id is None:  # if no file_id is given, we query ALL files
        cur.execute(sql_query)
    else:
        cur.execute(sql_query + " WHERE file_id LIKE ? ", (file_id + '%',))
    result = [{f: row[f] for f in fields}
              for row in cur.fetchall()]
    cur.close()
    return result


# Handle /file API endpoint
# POST: upload file
# GET: get list of all files on server
@app.route('/file', methods=['GET', 'POST'])
def get_all_files():
    if request.method == 'POST':
        return handle_upload(request)
    else:
        return jsonify(get_fileinfo(None)), 200


# Handle /file/<file_id> API endpoint
# GET: download single file
# DELETE: single file
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
            kwargs = { 'mimetype': result[0]['content_type']}
            if int(flask.__version__[0]) >= 2:  # because this was changed in Flask 2.0
                kwargs['download_name'] = os.path.basename(fname)
            else:
                kwargs['attachment_filename'] = os.path.basename(fname)
            return send_file(io.BytesIO(bindata), **kwargs)

    return make_response(("Unable to read data\n", 500))


# Add a file created by DeepLabCut to sqlite database (not uploaded from user)
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


# Actual task for analysing video with DeepLabCut and "hyökkäyslinja"
@celery.task
def analyse_video(fname, cfg_fname):
    import deeplabcut

    print('Starting analysis of video {} with model {}.'.format(fname, cfg_fname))
    res_id = deeplabcut.analyze_videos(cfg_fname,
                                       [data_path(fname)],
                                       save_as_csv=True)
    print(res_id)
    csv_fname = os.path.splitext(fname)[0] + res_id + '.csv'
    assert os.path.exists(data_path(csv_fname))

    hash_digest = add_results_file(csv_fname, "text/csv")

    return {'csv_file': hash_digest}

@celery.task
def analyse_video_mediapipe(fname, number):
    import cv2
    import mediapipe as mp
    import matplotlib.pyplot as plt
    import matplotlib
    import numpy as np
    import pandas as pd
    import time
    print('Starting analysis of video {} with MediaPipe BlazePose.'.format(fname))
 
    mp_drawing = mp.solutions.drawing_utils
    mp_drawing_styles = mp.solutions.drawing_styles
    mp_pose = mp.solutions.pose

    data = pd.DataFrame()

    frame = []
    nose_x = []
    nose_y = []
    left_shoulder_x = []
    left_shoulder_y = []
    right_shoulder_x = []
    right_shoulder_y = []
    left_elbow_x = []
    left_elbow_y = []
    right_elbow_x = []
    right_elbow_y = []
    left_wrist_x = []
    left_wrist_y = []
    right_wrist_x = []
    right_wrist_y = []
    left_hip_x = []
    left_hip_y = []
    right_hip_x = []
    right_hip_y = []
    left_knee_x = []
    left_knee_y = []
    right_knee_x = []
    right_knee_y = []
    left_ankle_x = []
    left_ankle_y = []
    right_ankle_x = []
    right_ankle_y = []
    left_toe_x = []
    left_toe_y = []
    right_toe_x = []
    right_toe_y = []

    com_x = []
    com_y = []

    #weights to COM calculation
    head_weight = 0.081
    forearm_weight = 0.022
    upperarm_weight = 0.028
    leg_weight = 0.0465
    foot_weight = 0.0145
    thight_weight = 0.100
    trunk_weight = 0.497
    
    input_video_path = data_path(fname)
    cap = cv2.VideoCapture(input_video_path)
    suc,frame_video = cap.read()
    x_shape = int(frame_video.shape[1])
    y_shape = int(frame_video.shape[0])
    print(x_shape)
    print(y_shape)
    #print(input_video_path)
    #print(fname + "fname")
    #print(os.path.splitext(fname)[0])
    folder_path = os.path.dirname(input_video_path)
    #print(folder_path)
    folder_path = os.path.dirname(folder_path)
    #print(folder_path)
    output_video_fname = (folder_path + "/" + os.path.splitext(fname)[0]  + '_labeled.mp4')
    #print(output_video_fname + " outputvideoname")
    output_csv_fname = (folder_path + "/" + os.path.splitext(fname)[0]  + '_results.csv')
    #print(output_csv_fname + " outputcsv")
    
    out = cv2.VideoWriter(output_video_fname, cv2.VideoWriter_fourcc('m', 'p', '4', 'v'), 100, (x_shape, y_shape))
    with mp_pose.Pose(
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5) as pose:
        while cap.isOpened():
            success, image = cap.read()
        
            if not success:
                print("Ignoring empty camera frame.")
                # If loading a video, use 'break' instead of 'continue'.
                break
        
            image.flags.writeable = False
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            results = pose.process(image)
        
            # Draw the pose annotation on the image.
            image.flags.writeable = True
            image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            mp_drawing.draw_landmarks(
                image,
                results.pose_landmarks,
                mp_pose.POSE_CONNECTIONS,
                landmark_drawing_spec=mp_drawing_styles.get_default_pose_landmarks_style())
            # Flip the image horizontally for a selfie-view display.
            #out.write(image)
 
            nose_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.NOSE].x)*x_shape
            nose_x.append(nose_x_value)
            nose_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.NOSE].y)*y_shape
            nose_y.append(nose_y_value)
        
            left_shoulder_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_SHOULDER].x)*x_shape
            left_shoulder_x.append(left_shoulder_x_value)
            left_shoulder_y_value =(results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_SHOULDER].y)*y_shape
            left_shoulder_y.append(left_shoulder_y_value)
            
            right_shoulder_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_SHOULDER].x)*x_shape
            right_shoulder_x.append(right_shoulder_x_value)
            right_shoulder_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_SHOULDER].y)*y_shape
            right_shoulder_y.append(right_shoulder_y_value)
            
            left_elbow_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_ELBOW].x)*x_shape
            left_elbow_x.append(left_elbow_x_value)
            left_elbow_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_ELBOW].y)*y_shape
            left_elbow_y.append(left_elbow_y_value)
            
            right_elbow_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_ELBOW].x)*x_shape
            right_elbow_x.append(right_elbow_x_value)
            right_elbow_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_ELBOW].y)*y_shape
            right_elbow_y.append(right_elbow_y_value)
            
            left_wrist_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_WRIST].x)*x_shape
            left_wrist_x.append(left_wrist_x_value)
            left_wrist_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_WRIST].y)*y_shape
            left_wrist_y.append(left_wrist_y_value)
            
            right_wrist_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_WRIST].x)*x_shape
            right_wrist_x.append(right_wrist_x_value)
            right_wrist_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_WRIST].y)*y_shape
            right_wrist_y.append(right_wrist_y_value)
            
            left_hip_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_HIP].x)*x_shape
            left_hip_x.append(left_hip_x_value)
            left_hip_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_HIP].y)*y_shape
            left_hip_y.append(left_hip_y_value)
            
            right_hip_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_HIP].x)*x_shape
            right_hip_x.append(right_hip_x_value)
            right_hip_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_HIP].y)*y_shape
            right_hip_y.append(right_hip_y_value)
            
            left_knee_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_KNEE].x)*x_shape
            left_knee_x.append(left_knee_x_value)
            left_knee_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_KNEE].y)*y_shape
            left_knee_y.append(left_knee_y_value)
            
            right_knee_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_KNEE].x)*x_shape
            right_knee_x.append(right_knee_x_value)
            right_knee_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_KNEE].y)*y_shape
            right_knee_y.append(right_knee_y_value)
            
            left_ankle_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_ANKLE].x)*x_shape
            left_ankle_x.append(left_ankle_x_value)
            left_ankle_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_ANKLE].y)*y_shape
            left_ankle_y.append(left_ankle_y_value)
            
            right_ankle_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_ANKLE].x)*x_shape
            right_ankle_x.append(right_ankle_x_value)
            right_ankle_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_ANKLE].y)*y_shape
            right_ankle_y.append(right_ankle_y_value)
            
            left_toe_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_FOOT_INDEX].x)*x_shape
            left_toe_x.append(left_toe_x_value)
            left_toe_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.LEFT_FOOT_INDEX].y)*y_shape
            left_toe_y.append(left_toe_y_value)
            
            right_toe_x_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_FOOT_INDEX].x)*x_shape
            right_toe_x.append(right_toe_x_value)
            right_toe_y_value = (results.pose_landmarks.landmark[mp_pose.PoseLandmark.RIGHT_FOOT_INDEX].y)*y_shape
            right_toe_y.append(right_toe_y_value)
            
            left_thight_x = left_hip_x_value + (0.433 * (left_knee_x_value - left_hip_x_value))
            left_thight_y = left_hip_y_value + (0.433 * (left_knee_y_value - left_hip_y_value))
            
            right_thight_x = right_hip_x_value + (0.433 * (right_knee_x_value - right_hip_x_value))
            right_thight_y = right_hip_y_value + (0.433 * (right_knee_y_value - right_hip_y_value))
            
            left_leg_x = left_knee_x_value + (0.606 * (left_knee_x_value - left_ankle_x_value))
            left_leg_y = left_knee_y_value + (0.606 * (left_knee_y_value - left_ankle_y_value))
            
            right_leg_x = right_knee_x_value + (0.606* (right_knee_x_value - right_ankle_x_value))
            right_leg_y = right_knee_y_value + (0.606* (right_knee_y_value - right_ankle_y_value))
            
            left_foot_x = left_ankle_x_value + (0.5 * (left_ankle_x_value - left_toe_x_value))
            left_foot_y = left_ankle_y_value + (0.5 * (left_ankle_y_value - left_toe_y_value))
            
            right_foot_x = right_ankle_x_value + (0.5 * (right_ankle_x_value - right_toe_x_value))
            right_foot_y = right_ankle_y_value + (0.5 * (right_ankle_y_value - right_toe_y_value))
        
            left_forearm_x = left_elbow_x_value + (0.682 * (left_elbow_x_value - left_wrist_x_value))
            left_forearm_y = left_elbow_y_value + (0.682 * (left_elbow_y_value - left_wrist_y_value))
            
            right_forearm_x = right_elbow_x_value + (0.682 * (right_elbow_x_value - right_wrist_x_value))
            right_forearm_y = right_elbow_y_value + (0.682 * (right_elbow_y_value - right_wrist_y_value))
            
            left_upperarm_x = left_shoulder_x_value + (0.436 * (left_shoulder_x_value - left_elbow_x_value))
            left_upperarm_y = left_shoulder_y_value + (0.436 * (left_shoulder_y_value - left_elbow_y_value))
            
            right_upperarm_x = right_shoulder_x_value + (0.436 * (right_shoulder_x_value - right_elbow_x_value))
            right_upperarm_y = right_shoulder_y_value + (0.436 * (right_shoulder_y_value - right_elbow_y_value))
            
            trunk_x = (left_shoulder_x_value + right_shoulder_x_value + left_hip_x_value + right_hip_x_value)/4
            trunk_y = (left_shoulder_y_value + right_shoulder_y_value + left_hip_y_value + right_hip_y_value)/4
            #both sides
        
            com_x_value = (head_weight * nose_x_value) + (forearm_weight*(left_forearm_x + right_forearm_x)) + (upperarm_weight*(left_upperarm_x + right_upperarm_x)) + (trunk_weight * trunk_x) + (thight_weight*(left_thight_x + right_thight_x)) + (leg_weight*(left_leg_x + right_leg_x)) + (foot_weight*(left_foot_x + right_foot_x))
            com_x.append(com_x_value)
            com_y_value = (head_weight * nose_y_value) + (forearm_weight*(left_forearm_y + right_forearm_y)) + (upperarm_weight*(left_upperarm_y + right_upperarm_y)) + (trunk_weight * trunk_y) + (thight_weight*(left_thight_y + right_thight_y)) + (leg_weight*(left_leg_y + right_leg_y)) + (foot_weight*(left_foot_y + right_foot_y))
            com_y.append(com_y_value)
            """
            #only left side
            com_x_value = (head_weight * nose_x_value) + (forearm_weight*(left_forearm_x + left_forearm_x)) + (upperarm_weight*(left_upperarm_x + left_upperarm_x)) + (trunk_weight * trunk_x) + (thight_weight*(left_thight_x + left_thight_x)) + (leg_weight*(left_leg_x + left_leg_x)) + (foot_weight*(left_foot_x + left_foot_x))
            com_x.append(com_x_value)
            com_y_value = (head_weight * nose_y_value) + (forearm_weight*(left_forearm_y + left_forearm_y)) + (upperarm_weight*(left_upperarm_y + left_upperarm_y)) + (trunk_weight * trunk_y) + (thight_weight*(left_thight_y + left_thight_y)) + (leg_weight*(left_leg_y + left_leg_y)) + (foot_weight*(left_foot_y + left_foot_y))
            com_y.append(com_y_value)
            """
            image = cv2.circle(image, (int(com_x_value), int(com_y_value)), radius=5, color=(0,255,0), thickness=-1)
            out.write(image)
        
        
            if cv2.waitKey(5) & 0xFF == 27:
                break
            

    data["nose_x"] = nose_x
    data["nose_y"] = nose_y
    data["left_shoulder_x"] = left_shoulder_x
    data["left_shoulder_y"] = left_shoulder_y
    data["right_shoulder_x"] = right_shoulder_x
    data["right_shoulder_y"] = right_shoulder_y
    data["left_elbow_x"] = left_elbow_x
    data["left_elbow_y"] = left_elbow_y
    data["right_elbow_x"] = right_elbow_x
    data["right_elbow_y"] = right_elbow_y
    data["left_wrist_x"] = left_wrist_x
    data["left_wrist_y"] = left_wrist_y
    data["right_wrist_x"] = right_wrist_x
    data["right_wrist_y"] = right_wrist_y
    data["left_hip_x"] = left_hip_x
    data["left_hip_y"] = left_hip_y
    data["right_hip_x"] = right_hip_x
    data["right_hip_y"] = right_hip_y
    data["left_knee_x"] = left_knee_x
    data["left_knee_y"] = left_knee_y
    data["right_knee_x"] = right_knee_x
    data["right_knee_y"] = right_knee_y
    data["left_ankle_x"] = left_ankle_x
    data["left_ankle_y"] = left_ankle_y
    data["right_ankle_x"] = right_ankle_x
    data["right_ankle_y"] = right_ankle_y
    data["left_toe_x"] = left_toe_x
    data["left_toe_y"] = left_toe_y
    data["right_toe_x"] = right_toe_x
    data["right_toe_y"] = right_toe_y
    data["com_x"] = com_x
    data["com_y"] = com_y

    data.to_csv(output_csv_fname)
    out.release()
    print("MediaPipe valmis")
    
    assert os.path.exists(data_path(output_csv_fname))

    hash_digest = add_results_file(output_csv_fname, "text/csv")

    return {'csv_file': hash_digest}
    
@celery.task 
def labeled_video_mediapipe(fname, number):
    input_video_path = data_path(fname)
    folder_path = os.path.dirname(input_video_path)
    folder_path = os.path.dirname(folder_path)
    output_video_fname = (folder_path + "/" + os.path.splitext(fname)[0]  + '_labeled.mp4')

    assert os.path.exists(data_path(output_video_fname))

    hash_digest = add_results_file(output_video_fname, "video/mp4")

    return {'labeled_video': hash_digest}
    
@celery.task
def analyse_image(fname, cfg_fname):
    import deeplabcut
    
    print('Starting analysis of image {} with model {}.'.format(fname, cfg_fname))
    
    print(data_path(fname))
    
    res_id = deeplabcut.analyze_time_lapse_frames(cfg_fname,
                                       [data_path(fname)][0],
                                       frametype='.jpg',
                                       save_as_csv=True)
    print(res_id)
    
    csv_fname = os.path.splitext(fname)[0] + res_id + '.csv'
    print(csv_fname)
    assert os.path.exists(data_path(csv_fname))
    
    hash_digest = add_results_file(csv_fname, "text/csv")
    
    return {'csv_file': hash_digest}

# Task for labelling video with DeepLabCut
@celery.task
def create_labeled_video_wassu_cam0(fname, cfg_fname):
    import deeplabcut

    print('Creating labeled video for {} with model {}.'.format(fname, cfg_fname))

    from deeplabcut.utils import auxiliaryfunctions

    deeplabcut.create_labeled_video(cfg_fname,
                                    [data_path(fname)],
                                    videotype='.mp4',
                                    draw_skeleton=True)

    cfg = auxiliaryfunctions.read_config(cfg_fname)
    res_id, _ = auxiliaryfunctions.GetScorerName(
        cfg, shuffle=1, trainFraction=cfg["TrainingFraction"][0])

    video_fname = os.path.splitext(fname)[0] + res_id + '_labeled.mp4'
    video_fname2 = os.path.splitext(fname)[0] + res_id + '_2.mp4'
    print(video_fname)
    #assert os.path.exists(data_path(video_fname))
    
    #hyökkäyslinjan piirtäminen
    import numpy as np
    import cv2
    import pandas as pd
    
    video_path = data_path(video_fname)
    print(res_id + "resid")
    print(os.path.splitext(fname)[0])
    print(video_path)
    cap = cv2.VideoCapture(video_path)
    
    folder_path = os.path.dirname(video_path)
    print(folder_path)
    
    folder_path = os.path.dirname(folder_path)
    print(folder_path)
    
    csv_name = os.path.splitext(fname)[0] + res_id + '.csv'
    csv_path = data_path(csv_name)
    csv = pd.read_csv(csv_path, header=2)
    
    tulos = (folder_path + "/" + os.path.splitext(fname)[0]  + res_id + '_2.mp4')
    
    print(tulos)
    
    output = cv2.VideoWriter(tulos, cv2.VideoWriter_fourcc('m', 'p', '4', 'v'), 100, (1280, 720))
    
    i = 0
    while(i < csv.shape[0]):
        #print("alku")
        ret, frame = cap.read()
        point1_x = int(csv._get_value(i, 28, takeable=True))
        point1_y = int(csv._get_value(i, 29, takeable=True))
        point2_x = int(csv._get_value(i, 19, takeable=True))
        point2_y = int(csv._get_value(i, 20, takeable=True))

        jakaja = (point2_x - point1_x)
        if jakaja != 0:
            k = (point2_y - point1_y) / jakaja
    
            point3_y = point1_y + 70
            point3_x = ((point3_y - point1_y) / k ) + point1_x

            point4_y = point2_y - 70
            point4_x = ((point4_y - point1_y) / k ) + point1_x
        
            img = cv2.line(frame, (int(point3_x), int(point3_y)), (int(point4_x), int(point4_y)), (0, 255, 0), 3)
        
        else:
            img = cv2.line(frame, (0, 0), (1, 1), (0, 255, 0), 7)
    
        output.write(img)
        
        i = i + 1    
        if cv2.waitKey(1) == ord('q'):
            break



    cap.release()
    cv2.destroyAllWindows()
    output.release()
    print("valmis")
    
    assert os.path.exists(data_path(video_fname))
    hash_digest = add_results_file(video_fname2, "video/mp4")

    return {'labeled_video': hash_digest}

# Task for labelling video with DeepLabCut
@celery.task
def create_labeled_video(fname, cfg_fname):
    import deeplabcut

    print('Creating labeled video for {} with model {}.'.format(fname, cfg_fname))

    from deeplabcut.utils import auxiliaryfunctions

    deeplabcut.create_labeled_video(cfg_fname,
                                    [data_path(fname)],
                                    draw_skeleton=True)

    cfg = auxiliaryfunctions.read_config(cfg_fname)
    res_id, _ = auxiliaryfunctions.GetScorerName(
        cfg, shuffle=1, trainFraction=cfg["TrainingFraction"][0])

    video_fname = os.path.splitext(fname)[0] + res_id + '_labeled.mp4'
    if not os.path.exists(data_path(video_fname)):
        video_fname = os.path.splitext(fname)[0] + res_id + 'filtered_labeled.mp4'

    assert os.path.exists(data_path(video_fname)), "Unable to find video file with name " +  video_fname

    hash_digest = add_results_file(video_fname, "video/mp4")

    return {'labeled_video': hash_digest}

# TODO
#    >>deeplabcut.triangulate(config_path3d, '/fullpath/videofolder', save_as_csv=True)
#    >>deeplabcut.create_labeled_video_3d(config_path3d, ['/fullpath/videofolder']

# Task for triangulate 3D coordinates
@celery.task
def triangulate(fname1, fname2, cfg_3d_name):
    import deeplabcut
    
    print('Creating 3D coordinates for {} and {} with model {}.'.format(fname1, fname2, cfg_3d_name))

    with open(cfg_3d_name) as fp:
        cfg_3d = yaml.safe_load(fp)

    camera_names = cfg_3d['camera_names']
    scorername = cfg_3d['scorername_3d']  # DLC_3D

    assert len(camera_names) == 2
    cn1 = camera_names[0]
    cn2 = camera_names[1]

    assert cn1 in fname1
    assert cn2 in fname2
    
    res_id = deeplabcut.triangulate(cfg_3d_name,
                                    [[data_path(fname1), data_path(fname2)]],
                                    save_as_csv=True)

    destfolder = os.path.dirname(data_path(fname1))
    vname = os.path.splitext(os.path.basename(fname2))[0]

    # The next lines are copied from deeplabcut.triangulate. Very
    # ugly, but the only way to get exactly the same output filename
    # as it doesn't return the filename...
    
    prefix = vname.split(cn2)[0]
    suffix = vname.split(cn2)[-1]
    if prefix == "":
        pass
    elif prefix[-1] == "_" or prefix[-1] == "-":
        prefix = prefix[:-1]
    
    if suffix == "":
        pass
    elif suffix[0] == "_" or suffix[0] == "-":
        suffix = suffix[1:]
    
    if prefix == "":
        output_file = os.path.join(destfolder, suffix)
    else:
        if suffix == "":
            output_file = os.path.join(destfolder, prefix)
        else:
            output_file = os.path.join(destfolder, prefix + "_" + suffix)
    
    csv_fname = output_file + "_" + scorername + ".csv"
    
    print(csv_fname)
    assert os.path.exists(data_path(csv_fname))
    
    hash_digest = add_results_file(csv_fname, "text/csv")
    
    return {'csv_file': hash_digest}


@celery.task
def label_image(fname, cfg_fname):
    import deeplabcut
    import cv2
    import os
    
    print('Creating labeled image {} with model {}.'.format(fname, cfg_fname))
    image_path = data_path(fname)
    folder_path = os.path.dirname(image_path)
    print(folder_path)
    files = os.listdir(folder_path)

    for file in files:
        if file.endswith(".csv"):
            with open(folder_path + "/" + file, mode='r', encoding='utf-8') as f:
                lines = f.readlines()
                i = 0
                for line in lines:
                    if i >= 3:
                        info = line.split(",")
                        #image name
                        image_name = fname.split("/")[1]
                        print(image_name)
                        #read detected points
                        piippu1_x = float(info[1])
                        piippu1_y = float(info[2])
                        piippu2_x = float(info[4])
                        piippu2_y = float(info[5])
                        haara_x =  float(info[7])
                        haara_y = float(info[8])
                        vasen_x = float(info[10])
                        vasen_y = float(info[11])
                        oikea_x = float(info[13])
                        oikea_y = float(info[14])

                        #read image
                        image = cv2.imread(image_path)
                    
                        #create points, color and thickness
                        piippu1 = (int(piippu1_x), int(piippu1_y))
                        piippu2 = (int(piippu2_x), int(piippu2_y))
                        haara = (int(haara_x), int(haara_y))
                        vasen = (int(vasen_x), int(vasen_y))
                        oikea = (int(oikea_x), int(oikea_y))
                        color = (0, 255, 0)
                        thickness = 4
                    
                        #draw lines to image
                        image = cv2.line(image, piippu1, piippu2, color, thickness)
                        image = cv2.line(image, piippu2, haara, color, thickness)
                        image = cv2.line(image, haara, vasen, color, thickness)
                        image = cv2.line(image, haara, oikea, color, thickness)
                        
                        image_name = folder_path + "/" + "labeled_" + image_name 
                        print(image_name)
                        #save image 
                        cv2.imwrite(image_name, image)
                    
                    i = i + 1
    
    assert os.path.exists(data_path(image_name))
    hash_digest = add_results_file(image_name, "image/jpg")
    
    return {'labeled_image': hash_digest}


# Dummy task for testing
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


# Get list of submitted analyses from sqlite database
def get_analysis_list():
    con = db.get_db()
    cur = con.cursor()

    fields = ['task_id', 'file_id', 'file2_id', 'analysis_name', 'state', 'created']
    cur.execute('SELECT id, {} FROM analyses'.format(', '.join(fields)))

    return [{f: row[f] for f in fields} for row in cur.fetchall()]


def get_file_from_db(file_id, cur):
    cur.execute('SELECT filename, file_id FROM files WHERE file_id LIKE ?', (file_id+'%',))
    found = cur.fetchone()
    return found['filename'] if found else None


# Handle /analysis API endpoint
# GET: get list of submitted analyses
# POST: add new analysis
@app.route('/analysis', methods=['GET', 'POST'])
def start_analysis():
    if request.method == 'GET':
        return jsonify(get_analysis_list()), 200

    # Check arguments
    if 'file_id' not in request.form:
        return make_response(("No file_id given\n", 400))
   
    file_id = request.form['file_id']
    if file_id == '':
    	return make_response(("No file_id given\n", 400))
    	
    analysis = request.form.get('analysis')

    dlc_model = request.form.get('model')

    if analysis is None:
        return make_response(("No analysis task given\n", 400))
        
    if analysis == '':
    	return make_response(("No analysis task given\n", 400))
    	
    if dlc_model == '':
    	return make_response(("No model task given\n", 400))
    analysis = analysis.lower()

    file2_id = None
    fname2 = None
    if analysis == 'triangulate':
        if 'file2_id' not in request.form:
            return make_response(("No file2_id given\n", 400))
        file2_id = request.form['file2_id']
        if file2_id == '':
    	    return make_response(("No file2_id given\n", 400))

    # Open database
    con = db.get_db()
    cur = con.cursor()

    # Find file_id in database
    fname = get_file_from_db(file_id, cur)
    if fname is None:
        return make_response(("Given file_id not found\n", 400))
    if not os.path.exists(data_path(fname)):
        return make_response(("File no longer exists in server\n", 400))

    if file2_id is not None:
        fname2 = get_file_from_db(file2_id, cur)
        if fname2 is None:
            return make_response(("Given file2_id not found\n", 400))
        if not os.path.exists(data_path(fname2)):
            return make_response(("File for file2_id no longer exists in server\n", 400))

    if analysis == 'sleep':
        sleep_time = request.form.get('time')
        if sleep_time is None:
            return make_response(("No time argument given\n", 400))
        task = analyse_sleep.apply_async(args=(fname, int(sleep_time)))
        
    elif analysis in ('video', 'label', 'image', 'label-image', 'triangulate'):
        if dlc_model is None:
            return make_response(("No model argument given. Supported models: {}.\n".format(
                ', '.join(app.config['DLC_MODELS'].keys())), 400))
        if dlc_model not in app.config['DLC_MODELS']:
            return make_response(("No model named '{}' found. Supported models: {}.\n".format(
                dlc_model, ', '.join(app.config['DLC_MODELS'].keys())), 400))
        if dlc_model == 'mediapipe':
            if analysis == 'video':
                task = analyse_video_mediapipe.apply_async(args=(fname, int(0)))
            elif analysis == 'label':
                task = labeled_video_mediapipe.apply_async(args=(fname, int(0)))
        else: 
            dlc_model_path = app.config['DLC_MODELS'][dlc_model]
            if analysis == 'video':
                task = analyse_video.apply_async(args=(fname, dlc_model_path))
            elif analysis == 'image':
                task = analyse_video.apply_async(args=(fname, dlc_model_path))
            elif (analysis == 'label') :
                if dlc_model == 'wassu-cam0':
                    task = create_labeled_video_wassu_cam0.apply_async(args=(fname, dlc_model_path))
                else:
                    task = create_labeled_video.apply_async(args=(fname, dlc_model_path))
            elif analysis == 'label-image':
                task = label_image.apply_async(args=(fname, dlc_model_path))
            elif analysis == 'triangulate':
                task = triangulate.apply_async(args=(fname, fname2, dlc_model_path))
    else:
        return make_response(("Unknown analysis task '{}'\n".format(analysis)),
                             400)

    task_id = task.id
    cur.execute("INSERT INTO analyses (task_id, file_id, file2_id, analysis_name, state) "
                "VALUES (?, ?, ?, ?, ?)", (task_id, fname, fname2, analysis, "PENDING"))
    con.commit()

    return jsonify({'task_id': task_id}), 202


# Handle /analysis/<task_id> endpoint
# Get status from analysis in progress, returns results if analysis is done
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


# Handle clean up, this can be run from command line with "flask clean-up"
# Deletes files older than config option clean_up_files_days
# Deletes analyses older than clean_up_analyses_days
@app.cli.command('clean-up')
def clean_up_command():
    clean_up_files_days = app.config['clean_up_files_days']
    clean_up_analyses_days = app.config['clean_up_analyses_days']

    print('Running clean up...')

    con = db.get_db()
    cur = con.cursor()

    cur.execute('SELECT * FROM analyses WHERE created < DATETIME("now", ?)',
                ('-{} days'.format(clean_up_analyses_days),))

    for row in cur.fetchall():
        task = analyse_video.AsyncResult(row['task_id'])
        if task.state in ('PENDING', 'STARTED'):
            print('WARNING: not deleting old task {} as status is {}'.format(
                row['task_id'], task.state))
        else:
            print('DELETING {} task {}, {}, {} ({})'.format(
                row['analysis_name'], row['task_id'], row['file_id'][:10], row['state'],
                row['created']))
            cur.execute('DELETE FROM analyses WHERE id = ?', (row['id'],))
            con.commit()
            task.forget()

    cur.execute('SELECT * FROM files WHERE created < DATETIME("now", ?)',
                ('-{} days'.format(clean_up_files_days),))

    for row in cur.fetchall():
        cur.execute('SELECT task_id FROM analyses WHERE file_id=?', (row['file_id'],))
        rows = cur.fetchall()
        if len(rows) > 0:
            print('WARNING: not deleting old file {} as it still has analyses:'.format(
                row['file_id']))
            for r in rows:
                print('  ', r[0])
        else:
            print('DELETING file {}, {} ({})'.format(row['file_id'], row['filename'],
                                                     row['created']))
            cur.execute('DELETE FROM files WHERE id = ?', (row['id'],))
            con.commit()

            real_fname = data_path(row['filename'])
            try:
                os.remove(real_fname)
            except FileNotFoundError:
                print('WARNING: file to be removed not found: {}'.format(real_fname))
