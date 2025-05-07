import os
import json
import jwt
import requests
from datetime import datetime, timedelta
from flask import Flask, request, session, redirect, jsonify, send_from_directory
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = os.getenv('FLASK_SECRET_KEY')
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Spotify OAuth Endpoints
@app.route('/spotify_login')
def spotify_login():
    state = os.urandom(16).hex()
    session['state'] = state
    params = {
        'client_id': os.getenv('SPOTIFY_CLIENT_ID'),
        'response_type': 'code',
        'redirect_uri': os.getenv('SPOTIFY_REDIRECT_URI'),
        'state': state,
        'scope': 'playlist-read-private'
    }
    return redirect(f"https://accounts.spotify.com/authorize?{requests.compat.urlencode(params)}")

@app.route('/callback')
def spotify_callback():
    if request.args.get('error'):
        return redirect('/?error=spotify_auth_failed')
    
    code = request.args.get('code')
    state = request.args.get('state')
    
    if state != session.get('state'):
        return redirect('/?error=state_mismatch')

    # Exchange code for token
    response = requests.post('https://accounts.spotify.com/api/token', data={
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': os.getenv('SPOTIFY_REDIRECT_URI'),
        'client_id': os.getenv('SPOTIFY_CLIENT_ID'),
        'client_secret': os.getenv('SPOTIFY_CLIENT_SECRET')
    })
    
    if response.status_code != 200:
        return redirect('/?error=token_exchange_failed')
    
    session['spotify_token'] = response.json().get('access_token')
    return redirect('/')

# Apple Music Integration
@app.route('/apple-token')
def generate_apple_token():
    try:
        now = datetime.utcnow()
        token = jwt.encode(
            {
                'iss': os.getenv('APPLE_TEAM_ID'),
                'iat': now,
                'exp': now + timedelta(hours=1)
            },
            os.getenv('APPLE_PRIVATE_KEY').replace('\\n', '\n'),
            algorithm='ES256',
            headers={'kid': os.getenv('APPLE_KEY_ID')}
        )
        return jsonify({'token': token})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Playlist Transfer Logic
@app.route('/transfer', methods=['POST'])
def transfer_playlist():
    try:
        data = request.json
        spotify_url = data.get('playlist_url')
        apple_token = data.get('apple_token')
        
        # Validate inputs
        if not spotify_url or not apple_token:
            return jsonify({'error': 'Missing required parameters'}), 400

        # Extract Spotify playlist ID
        playlist_id = spotify_url.split('/')[-1].split('?')[0]
        spotify_tracks = get_spotify_tracks(playlist_id)
        
        # Process tracks
        apple_tracks = []
        missing_tracks = []
        
        for idx, track in enumerate(spotify_tracks):
            apple_id = search_apple_music(
                f"{track['name']} {track['artist']}",
                apple_token
            )
            if apple_id:
                apple_tracks.append(apple_id)
            else:
                missing_tracks.append(f"{track['artist']} - {track['name']}")
        
        # Create Apple Music playlist
        created = create_apple_playlist(
            "Spotify Import",
            apple_tracks,
            apple_token
        )
        
        return jsonify({
            'transferred': len(apple_tracks),
            'missing': missing_tracks,
            'playlist_url': created.get('attributes', {}).get('url', '#')
        })
    
    except Exception as e:
        app.logger.error(f"Transfer error: {str(e)}")
        return jsonify({'error': 'Playlist transfer failed'}), 500

# Spotify API Helper
def get_spotify_tracks(playlist_id):
    try:
        headers = {'Authorization': f"Bearer {session['spotify_token']}"}
        response = requests.get(
            f'https://api.spotify.com/v1/playlists/{playlist_id}/tracks',
            headers=headers
        )
        response.raise_for_status()
        
        return [{
            'name': item['track']['name'],
            'artist': item['track']['artists'][0]['name']
        } for item in response.json()['items']]
    
    except Exception as e:
        app.logger.error(f"Spotify API error: {str(e)}")
        raise

# Apple Music API Helpers
def search_apple_music(query, apple_token):
    try:
        headers = {
            'Authorization': f"Bearer {apple_token}",
            'Music-User-Token': apple_token
        }
        response = requests.get(
            f'https://api.music.apple.com/v1/catalog/us/search?types=songs&limit=1&term={query}',
            headers=headers
        )
        response.raise_for_status()
        
        return response.json()['results']['songs']['data'][0]['id'] if response.json().get('results') else None
    
    except Exception:
        return None

def create_apple_playlist(name, track_ids, apple_token):
    try:
        headers = {
            'Authorization': f"Bearer {apple_token}",
            'Music-User-Token': apple_token,
            'Content-Type': 'application/json'
        }
        data = {
            'attributes': {
                'name': name,
                'description': 'Imported from Spotify via TrackFade'
            },
            'relationships': {
                'tracks': {
                    'data': [{'id': id, 'type': 'songs'} for id in track_ids]
                }
            }
        }
        response = requests.post(
            'https://api.music.apple.com/v1/me/library/playlists',
            json=data,
            headers=headers
        )
        response.raise_for_status()
        return response.json()['data'][0]
    
    except Exception as e:
        app.logger.error(f"Apple Music API error: {str(e)}")
        raise

# Auth Status Endpoint
@app.route('/check_spotify')
def check_spotify_auth():
    return jsonify({'authenticated': 'spotify_token' in session})

@app.route('/spotify_logout')
def spotify_logout():
    session.pop('spotify_token', None)
    return jsonify({'success': True})

# Serve Frontend
@app.route('/')
def serve_frontend():
    return send_from_directory('static', 'index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
