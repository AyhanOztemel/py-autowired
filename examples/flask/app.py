from flask import Flask

from bootstrap import bootstrap
from presentation.routes import blueprint

app = Flask(__name__)
app.register_blueprint(blueprint)
bootstrap(app)

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8002)
