from web import create_app
try:
    app = create_app()
    print('App created:', app)
except Exception as e:
    print('Error:', e)
    import traceback
    traceback.print_exc()