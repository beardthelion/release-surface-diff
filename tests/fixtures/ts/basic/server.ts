import express from "express";

const app = express();
const api = express.Router();

app.use(requireAuth);
app.use("/api", api);

function requireAuth(req, res, next) {
  next();
}

function getH(req, res) {
  res.send("ok");
}

function createItem(req, res) {
  res.send("created");
}

function getThing(req, res) {
  res.send(req.params.tid);
}

app.get("/h/:id", getH);
app.post("/items", createItem);
app.route("/r").get(getH).post(createItem);
api.get("/things/:tid", getThing);
