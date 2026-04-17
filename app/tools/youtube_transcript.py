TOOL_LIBRARY_NAME = 'YouTube Transcript'
TOOL_LIBRARY_DESC = 'Extract transcripts and subtitles from YouTube videos.'
TOOL_LIBRARY_ICON = '▶️'

def init_tool_library() -> None:
    import pipmaster as pm
    pm.ensure_packages({'youtube-transcript-api': '>=0.6.2'})

def tool_get_youtube_transcript(args: dict, lollms=None):
    '''
    Retrieve the transcript for a YouTube video.
    
    Args:
        args: dict with keys:
            - video_url (str): The full URL or video ID of the YouTube video.
            - language (str, optional): The preferred language code (e.g., 'en', 'fr'). Default is 'en'.
    '''
    try:
        url_or_id = args.get('video_url', '')
        lang = args.get('language', 'en')
        
        if not url_or_id:
            return "Error: No video URL or ID provided."
            
        # Extract video ID if it's a URL
        video_id = url_or_id
        if "v=" in url_or_id:
            video_id = url_or_id.split("v=")[-1].split("&")[0]
        elif "youtu.be/" in url_or_id:
            video_id = url_or_id.split("youtu.be/")[-1].split("?")[0]
            
        from youtube_transcript_api import YouTubeTranscriptApi
        
        try:
            transcript = YouTubeTranscriptApi.get_transcript(video_id, languages=[lang])
            text = " ".join([t['text'] for t in transcript])
            return f"--- TRANSCRIPT FOR VIDEO ID {video_id} ({lang}) ---\n\n{text}"
        except Exception as trans_e:
            return f"Could not fetch transcript: {str(trans_e)}"
            
    except Exception as e:
        return f"Execution Error: {str(e)}"