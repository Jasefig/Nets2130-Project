import logging
from flask_cors import CORS
from flask import request, jsonify, Flask, render_template, url_for, Response
import sys
import os
import uuid
from config import *
from datetime import datetime
import mysql.connector
import boto3
import requests
import openai
import base64
from io import BytesIO
from PIL import Image
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
CORS(app)
bucket_name = 'individualwork'

### routes for pages ###


@app.route('/')
def test():
    return render_template('index.html')


@app.route('/generate')
def generate():
    # get the seed image URL based on the day
    seed_image_url = get_seed_image()
    return render_template(
        'pages/generate.html',
        seed_image_url=seed_image_url)


@app.route('/vote')
def vote():
    return render_template('pages/vote.html')


@app.route('/goodbye')
def goodbye():
    return render_template('pages/goodbye.html')


### helper functions ###

def get_db_connection():
    """Create a connection to the RDS database"""
    return mysql.connector.connect(
        host=RDS_HOST,
        port=RDS_PORT,
        database=RDS_DATABASE,
        user=RDS_USERNAME,
        password=RDS_PASSWORD
    )


def get_seed_image():
    """Get the seed image URL for the current day or default seed image."""
    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        today = datetime.now().date()
        logging.info('Today date: ' + str(today))

        # check if there are any previous days with images
        cursor.execute("""
            SELECT DISTINCT day FROM Image
            WHERE day < %s
            ORDER BY day DESC
            LIMIT 1
        """, (today,))
        day_row = cursor.fetchone()

        if day_row:
            previous_day = day_row['day']
            logging.info('previous day date: ' + str(previous_day))
            # find the image with the highest upvotes for that day
            cursor.execute("""
                SELECT s3_path FROM Image
                WHERE day = %s
                ORDER BY upvotes DESC, downvotes ASC, created_at ASC
                LIMIT 1
            """, (previous_day,))
            image_row = cursor.fetchone()
            if image_row:
                s3_path = image_row['s3_path']
                # use the proxy URL instead of direct S3 URL - addresses CORS
                # issue
                seed_image_url = (
                    f"/proxy-image?url=https://{bucket_name}.s3.amazonaws.com/{s3_path}")
                logging.info(f"Seed image URL: {seed_image_url}")
                return seed_image_url

        # if no previous images or error, return default seed image
        seed_image_url = url_for(
            'static', filename='data/seed_image.jpg', _external=True)
        return seed_image_url

    except Exception as e:
        logging.error(f"Error fetching seed image: {e}")
        # return default seed image in case of error
        seed_image_url = url_for(
            'static', filename='data/seed_image.jpg', _external=True)
        return seed_image_url

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db_conn' in locals():
            db_conn.close()



def insert_day(image_id, today):
    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor()

        logging.info("Database successfully connected")
        logging.info(f'image_id: {image_id}')
        logging.info(f'today date: {today}')

        # first insert with NULL seed_image_id if record doesn't exist
        cursor.execute("""
            INSERT INTO Day (date, seed_image_id, total_votes, total_participants, is_current)
            SELECT %s, NULL, 0, 0, TRUE
            WHERE NOT EXISTS (SELECT 1 FROM Day WHERE date = %s)
        """, (today, today))

        db_conn.commit()
        logging.info(f"Successfully added Day record for today: {today}")

    except Exception as db_error:
        logging.error(f"Database error: {db_error}")
        raise
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db_conn' in locals():
            db_conn.close()


def process_mask_for_dalle(mask_data_url):
    """
    Process a mask from canvas data URL into the format DALL-E 2 expects:
    - Transparent (alpha=0) for areas to edit
    - Solid black (alpha=255) for areas to preserve
    Returns mask in RGBA format
    """
    # Decode mask image from base64
    header, encoded = mask_data_url.split(",", 1)
    mask_data = base64.b64decode(encoded)
    mask_image = Image.open(BytesIO(mask_data)).convert("RGBA")

    # Get alpha channel
    alpha = mask_image.split()[3]

    # Create new RGBA image
    # Solid black with full alpha
    final_mask = Image.new('RGBA', mask_image.size, (0, 0, 0, 255))

    # Create white areas with zero alpha where user drew
    transparent_areas = Image.new('RGBA', mask_image.size, (0, 0, 0, 0))
    final_mask.paste(transparent_areas, mask=Image.eval(
        alpha, lambda x: 255 if x == 0 else 0))

    return final_mask


### endpoints ###

@app.route('/generate-image', methods=['POST'])
def generate_image_endpoint():
    logging.info("Endpoint /generate-image was hit")

    data = request.get_json()
    prompt = data.get('prompt')
    mask_data_url = data.get('mask')
    seed_image_url = data.get('seedImage')
    # format example: 11/30/2024, 11:29:07 PM
    created_at = data.get('createdAt')

    # extract correct dates for database
    date_obj = datetime.strptime(created_at, "%m/%d/%Y, %I:%M:%S %p")
    formatted_created_at = date_obj.strftime("%Y-%m-%d %H:%M:%S")
    today = date_obj.strftime("%Y-%m-%d")

    if not all([prompt, mask_data_url, seed_image_url]):
        return jsonify({'error': 'Missing required parameters'}), 400

    try:
        # validate credentials
        validate_env()
        openai.api_key = OPENAI_API_KEY

        # get seed image
        if 'static/data/seed_image.jpg' in seed_image_url:
            static_file_path = os.path.join(
                app.static_folder, 'data', 'seed_image.jpg')
            with open(static_file_path, 'rb') as f:
                seed_image_data = f.read()
        else:
            s3_path = seed_image_url.split(
                '/proxy-image?url=https://' + bucket_name + '.s3.amazonaws.com/')[-1]
            s3_client = boto3.client(
                's3',
                aws_access_key_id=AWS_ACCESS_KEY_ID,
                aws_secret_access_key=AWS_SECRET_ACCESS_KEY)
            response = s3_client.get_object(Bucket=bucket_name, Key=s3_path)
            seed_image_data = response['Body'].read()

        # process seed image (keep as RGBA)
        seed_image = Image.open(BytesIO(seed_image_data)).convert("RGBA")
        seed_image = seed_image.resize((512, 512))

        # process mask image
        mask_image = process_mask_for_dalle(mask_data_url)
        mask_image = mask_image.resize((512, 512))

        # save images to bytes
        seed_bytes = BytesIO()
        mask_bytes = BytesIO()

        seed_image.save(seed_bytes, format='PNG')
        mask_image.save(mask_bytes, format='PNG')

        seed_bytes.seek(0)
        mask_bytes.seek(0)

        # call DALL-E 2 API
        response = openai.images.edit(
            model="dall-e-2",
            image=seed_bytes,
            mask=mask_bytes,
            prompt=prompt,
            n=1,
            size="512x512"
        )

        # process response and save to S3
        image_url = response.data[0].url
        logging.info(f"Generated image URL: {image_url}")

        # download generated image
        image_response = requests.get(image_url)
        if image_response.status_code != 200:
            raise Exception(f"Failed to download generated image: {image_response.status_code}")  # noqa: E501
        else:
            logging.info(f"Image successfully generated by Dalle 2")

        # generate unique ID and S3 path
        image_id = str(uuid.uuid4())
        s3_path = f'daily-submissions/{today}/{image_id}.png'

        # upload to S3
        s3_client = boto3.client('s3',
                                 aws_access_key_id=AWS_ACCESS_KEY_ID,
                                 aws_secret_access_key=AWS_SECRET_ACCESS_KEY
                                 )
        s3_client.put_object(
            Bucket=bucket_name,
            Key=s3_path,
            Body=image_response.content,
            ContentType='image/png',
        )

        # insert day record
        insert_day(image_id, today)

        # return success response
        full_image_url = f"https://{bucket_name}.s3.amazonaws.com/{s3_path}"
        return jsonify({
            'imageUrl': full_image_url,
            'image_id': image_id,
            'day': today,
            'created_at': formatted_created_at
        }), 200

    except openai.OpenAIError as e:
        logging.error(f"OpenAI API error: {e}")
        return jsonify({'error': str(e)}), 500
    except Exception as e:
        logging.error(f"Error in generate_image_endpoint: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/track-user', methods=['POST'])
def track_user():
    logging.info("Endpoint /track-user was hit")
    data = request.get_json()
    user_id = data.get('user_id')
    created_at = data.get('created_at')

    if not user_id or not created_at:
        return jsonify({'error': 'Missing user_id or created_at'}), 400

    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor()

        logging.info("Database successfully connected")
        logging.info(f'user_id: {user_id}')
        # reformat date so it can be referenced by foreign keys
        date_obj = datetime.strptime(created_at, "%m/%d/%Y, %I:%M:%S %p")
        created_at = date_obj.strftime("%Y-%m-%d %H:%M:%S")
        logging.info(f'created_at: {created_at}')

        # Insert or update the user in the database
        cursor.execute("""
            INSERT INTO User (user_id, username, created_at, is_banned)
            VALUES (%s, %s, %s, FALSE)
        """, (user_id, 'Unknown', created_at))
        db_conn.commit()

        logging.info("User tracked successfully!")

        return jsonify({'message': 'User tracked successfully'}), 200
    except Exception as e:
        logging.error(f"Error tracking user: {e}")
        return jsonify({'error': 'Failed to track user or user already exists in db'}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db_conn' in locals():
            db_conn.close()


@app.route('/insert-image', methods=['POST'])
def insert_image():
    logging.info("Endpoint /insert-image was hit")
    data = request.get_json()

    image_id = data.get('image_id')
    s3_path = data.get('s3_path')
    prompt_text = data.get('prompt_text')
    creator_id = data.get('creator_id')
    day = data.get('day')
    created_at = data.get('created_at')
    upvotes = data.get('upvotes', 0)
    downvotes = data.get('downvotes', 0)
    flags = data.get('flags', 0)

    logging.info(f'image_id: {image_id}')
    logging.info(f's3_path: {s3_path}')
    logging.info(f'creator_id: {creator_id}')
    logging.info(f'day: {day}')

    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor()

        logging.info("Database successfully connected")

        cursor.execute("""
            INSERT INTO Image (
                image_id,
                s3_path,
                prompt_text,
                created_at,
                creator_id,
                day,
                upvotes,
                downvotes,
                flags
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (image_id, s3_path, prompt_text, created_at, creator_id, day, upvotes, downvotes, flags))

        # Update the seed_image_id in the Day table if it's NULL 
        cursor.execute("""
            UPDATE Day
            SET seed_image_id = %s
            WHERE date = %s AND seed_image_id IS NULL
        """, (image_id, day))

        db_conn.commit()

        logging.info("Successfully loaded into Image table!")

        return jsonify({'message': 'Image inserted successfully'}), 201

    except Exception as e:
        logging.error(f"Error inserting image into database: {e}")
        return jsonify({'error': 'Failed to insert image into database'}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db_conn' in locals():
            db_conn.close()


@app.route('/get-images', methods=['GET'])
def get_images():
    logging.info("Endpoint /get-images was hit")
    day = request.args.get('day')

    if not day:
        return jsonify({'message': 'Day is required'}), 400
    
    date_obj = datetime.strptime(day, "%m/%d/%Y, %I:%M:%S %p")
    day = date_obj.strftime("%Y-%m-%d")
    logging.info(f"Day in get-images endpoint: {day}") # should be in YYYY-MM-DD format

    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        logging.info("Database successfully connected")

        # query for images for the given day
        cursor.execute("""
            SELECT image_id, s3_path, prompt_text, upvotes, downvotes
            FROM Image
            WHERE day = %s
            ORDER BY created_at DESC
        """, (day,))

        images = cursor.fetchall()

        if images:
            return jsonify({'images': images}), 200
        else:
            return jsonify(
                {'message': 'Looks like there were no images from today!'}), 200

    except Exception as e:
        logging.error(f"Error fetching images: {e}")
        return jsonify({'error': 'Failed to fetch images'}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db_conn' in locals():
            db_conn.close()


@app.route('/vote-image', methods=['POST'])
def vote_image():
    data = request.get_json()
    image_id = data.get('image_id')
    current_vote = data.get('current_vote')  # -1, 0, +1
    new_vote = data.get('new_vote')  # -1, 0, +1

    if not image_id or current_vote not in [-1, 0, 1] or new_vote not in [-1, 0, 1]:
        return jsonify({'error': 'Invalid image ID or vote values'}), 400

    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor()

        # Calculate the changes in upvotes and downvotes
        upvote_change = 0
        downvote_change = 0

        if current_vote == 0 and new_vote == 1:
            upvote_change = 1
        elif current_vote == 1 and new_vote == 0:
            upvote_change = -1
        elif current_vote == 1 and new_vote == -1:
            upvote_change = -1
            downvote_change = 1
        elif current_vote == 0 and new_vote == -1:
            downvote_change = 1
        elif current_vote == -1 and new_vote == 0:
            downvote_change = -1
        elif current_vote == -1 and new_vote == 1:
            downvote_change = -1
            upvote_change = 1

        # Update votes for the image
        cursor.execute(
            "UPDATE Image SET upvotes = GREATEST(0, upvotes + %s), downvotes = GREATEST(0, downvotes + %s) WHERE image_id = %s",
            (upvote_change, downvote_change, image_id)
        )

        # Get the day for this image
        cursor.execute(
            "SELECT day FROM Image WHERE image_id = %s", (image_id,))
        day_result = cursor.fetchone()
        if day_result:
            day = day_result[0]

            # Find the image with the highest upvotes for this day
            cursor.execute("""
                SELECT image_id FROM Image 
                WHERE day = %s 
                ORDER BY upvotes DESC, downvotes ASC, created_at ASC 
                LIMIT 1
            """, (day,))
            highest_voted = cursor.fetchone()

            if highest_voted:
                # Update the Day table with the highest voted image
                cursor.execute("""
                    UPDATE Day 
                    SET seed_image_id = %s 
                    WHERE date = %s
                """, (highest_voted[0], day))

        db_conn.commit()
        return jsonify({'message': 'Vote recorded successfully'}), 200

    except Exception as e:
        logging.error(f"Error voting on image: {e}")
        return jsonify({'error': 'Failed to record vote'}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db_conn' in locals():
            db_conn.close()


@app.route('/proxy-image')
def proxy_image():
    image_url = request.args.get('url')
    if not image_url:
        return 'No URL provided', 400

    try:
        # parse the S3 path from the full URL
        s3_path = image_url.split('amazonaws.com/')[-1].split('?')[0]

        # get the image from S3
        s3_client = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY
        )

        response = s3_client.get_object(
            Bucket=bucket_name,
            Key=s3_path
        )

        # return the image with proper headers
        return Response(
            response['Body'].read(),
            mimetype='image/png',
            headers={
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0'
            }
        )
    except Exception as e:
        logging.error(f"Error proxying image: {e}")
        return 'Error fetching image', 500


@app.route('/update-vote-count', methods=['POST'])
def update_vote_count():
    data = request.get_json()
    # +1 for upvote/downvote, -1 for deselect
    increment = data.get('increment')
    if increment not in [1, -1]:
        return jsonify({'error': 'Invalid vote increment'}), 400

    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor()

        # Get today's date
        today = datetime.now().strftime('%Y-%m-%d')
        logging.info(
            f"Updating total_votes for date: {today} "
            f"with increment: {increment}"
        )

        # Update total_votes for the current day
        cursor.execute("""
            UPDATE Day SET total_votes = total_votes + %s
            WHERE date = %s
        """, (increment, today))
        db_conn.commit()

        logging.info("Total votes updated successfully")
        return jsonify({'message': 'Vote count updated successfully'}), 200

    except Exception as e:
        logging.error(f"Error updating vote count: {e}", exc_info=True)
        return jsonify({'error': 'Failed to update vote count'}), 500

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db_conn' in locals():
            db_conn.close()


@app.route('/increment-participant', methods=['POST'])
def increment_participant():
    data = request.get_json()
    user_id = data.get('user_id')
    created_at = data.get('created_at')

    # extract correct dates for database
    date_obj = datetime.strptime(created_at, "%m/%d/%Y, %I:%M:%S %p")
    today = date_obj.strftime("%Y-%m-%d")

    if not user_id:
        return jsonify({'error': 'User ID is required'}), 400

    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor()

        # Check if the user has generated at least one image
        cursor.execute("""
            SELECT COUNT(*) FROM Image WHERE creator_id = %s
        """, (user_id,))
        image_count = cursor.fetchone()[0]

        if image_count > 0:
            # Increment participant count
            cursor.execute("""
                UPDATE Day SET total_participants = total_participants + 1 WHERE date = %s
            """, (today,))
            db_conn.commit()
            return jsonify(
                {'message': 'Participant incremented successfully'}), 200
        else:
            return jsonify(
                {'message': 'User has not generated any images'}), 400

    except Exception as e:
        logging.error(f"Error incrementing participant: {e}")
        return jsonify({'error': 'Failed to increment participant'}), 500

    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db_conn' in locals():
            db_conn.close()


@app.route('/get-history')
def get_history():
    try:
        db_conn = get_db_connection()
        cursor = db_conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT d.date, i.s3_path
            FROM Day d
            JOIN Image i ON d.seed_image_id = i.image_id
            WHERE d.seed_image_id IS NOT NULL
            ORDER BY d.date DESC
        """)

        history = cursor.fetchall()
        return jsonify({'history': history}), 200

    except Exception as e:
        logging.error(f"Error fetching history: {e}")
        return jsonify({'error': 'Failed to fetch history'}), 500
    finally:
        if 'cursor' in locals():
            cursor.close()
        if 'db_conn' in locals():
            db_conn.close()


if __name__ == '__main__':
    app.run(debug=True)
