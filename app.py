import os
import json
import jwt
import requests
from datetime import datetime, timedelta
from flask import Flask, request, session, redirect, jsonify, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = os.getenv('FLASK_SECRET_KEY')
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_HTTPONLY=True,
    PREFERRED_URL_SCHEME='https'
)

CORS(app, 
     supports_credentials=True,
     origins=os.getenv('FRONTEND_URL', 'http://localhost:5000').split(','))

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
    try:
        if request.args.get('error'):
            return redirect('/?error=spotify_auth_failed')
        
        code = request.args.get('code')
        state = request.args.get('state')
        
        if state != session.get('state'):
            return redirect('/?error=state_mismatch')

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

    except Exception as e:
        app.logger.error(f"Spotify callback error: {str(e)}")
        return redirect('/?error=auth_failed')

# Apple Music Integration
@app.route('/apple-token')
def generate_apple_token():
    try:
        now = datetime.utcnow()
        token_payload = {
            'iss': os.getenv('APPLE_TEAM_ID'),
            'iat': now,
            'exp': now + timedelta(hours=1)
        }

        private_key = os.getenv('APPLE_PRIVATE_KEY').replace('\\n', '\n')
        
        if not private_key.startswith('-----BEGIN PRIVATE KEY-----'):
            raise ValueError('Invalid private key format')

        token = jwt.encode(
            token_payload,
            private_key,
            algorithm='ES256',
            headers={
                'kid': os.getenv('APPLE_KEY_ID'),
                'alg': 'ES256'
            }
        )
        
        return jsonify({'token': token})
        
    except Exception as e:
        app.logger.error(f"Apple token generation failed: {str(e)}")
        return jsonify({
            'error': 'Apple Music config error',
            'debug': {
                'team_id_set': bool(os.getenv('APPLE_TEAM_ID')),
                'key_id_set': bool(os.getenv('APPLE_KEY_ID')),
                'private_key_valid': 'BEGIN PRIVATE KEY' in os.getenv('APPLE_PRIVATE_KEY', '')
            }
        }), 500

@app.route('/.well-known/apple-app-site-association')
def apple_app_site_association():
    return jsonify({
        "applinks": {
            "apps": [],
            "details": [
                {
                    "appID": f"{os.getenv('APPLE_TEAM_ID')}.com.trackfade.musickit",
                    "paths": ["*"]
                }
            ]
        }
    }), 200, {'Content-Type': 'application/json'}

# Playlist Transfer Logic
@app.route('/transfer', methods=['POST'])
def transfer_playlist():
    try:
        apple_token = request.headers.get('Apple-Music-User-Token')
        data = request.json
        
        if not data or 'playlist_url' not in data:
            return jsonify({'error': 'Missing playlist URL'}), 400
            
        if not apple_token or not validate_apple_token(apple_token):
            return jsonify({'error': 'Invalid Apple Music token'}), 401

        spotify_url = data['playlist_url']

        try:
            playlist_id = spotify_url.split('/playlist/')[1].split('?')[0]
        except IndexError:
            return jsonify({'error': 'Invalid Spotify playlist URL'}), 400

        try:
            spotify_tracks = get_spotify_tracks(playlist_id)
        except Exception as e:
            return jsonify({'error': f'Spotify API error: {str(e)}'}), 500

        apple_tracks = []
        missing_tracks = []
        
        for track in spotify_tracks:
            try:
                apple_id = search_apple_music(
                    f"{track['name']} {track['artist']}",
                    apple_token
                )
                if apple_id:
                    apple_tracks.append(apple_id)
                else:
                    missing_tracks.append(f"{track['artist']} - {track['name']}")
            except Exception as e:
                missing_tracks.append(f"{track['artist']} - {track['name']}")

        try:
            created = create_apple_playlist(
                "Imported from Spotify",
                apple_tracks,
                apple_token
            )
            playlist_url = created.get('attributes', {}).get('url', '#')
        except Exception as e:
            return jsonify({'error': f'Apple Music API error: {str(e)}'}), 500

        return jsonify({
            'transferred': len(apple_tracks),
            'missing': missing_tracks,
            'playlist_url': playlist_url
        })
    
    except Exception as e:
        app.logger.error(f"Transfer failed: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

def validate_apple_token(token):
    try:
        response = requests.get(
            'https://api.music.apple.com/v1/me/storefront',
            headers={'Authorization': f"Bearer {token}"}
        )
        return response.status_code == 200
    except:
        return False

# Spotify API Helper
def get_spotify_tracks(playlist_id):
    try:
        if 'spotify_token' not in session:
            raise ValueError("No Spotify session found")
        
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
            f'https://api.music.apple.com/v1/catalog/us/search?types=songs&limit=1&term={requests.utils.quote(query)}',
            headers=headers
        )
        response.raise_for_status()
        
        if response.json().get('results', {}).get('songs', {}).get('data'):
            return response.json()['results']['songs']['data'][0]['id']
        return None
        
    except Exception as e:
        app.logger.error(f"Apple search failed for {query}: {str(e)}")
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

# Error handling
@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Endpoint not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500

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
