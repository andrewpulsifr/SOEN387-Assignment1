from pathlib import Path

from flask import Flask, send_from_directory

from routes import api_routes

app = Flask(__name__)
HERE = Path(__file__).resolve().parent

app.register_blueprint(api_routes)


@app.get("/openapi.yaml")
def openapi_yaml():
    return send_from_directory(HERE / "contracts", "encore-openapi.yaml", mimetype="text/yaml")


@app.get("/apidocs")
@app.get("/apidocs/")
def apidocs():
    return """
<!doctype html>
<html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Encore API Docs</title>
        <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
    </head>
    <body>
        <div id="swagger-ui"></div>
        <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
        <script>
            window.ui = SwaggerUIBundle({
                url: '/openapi.yaml',
                dom_id: '#swagger-ui',
                deepLinking: true,
                presets: [SwaggerUIBundle.presets.apis],
                layout: 'BaseLayout'
            });
        </script>
    </body>
</html>
"""


if __name__ == "__main__":
    app.run(port=3000, threaded=True)
