import React from "react";
import { createRoot } from "react-dom/client";
import GuideApp from "./GuideApp.jsx";
import "../vendor/omnibioai-design-tokens/tokens.css";
import "./guide.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <GuideApp />
  </React.StrictMode>,
);
