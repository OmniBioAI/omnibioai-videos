import React from "react";
import { createRoot } from "react-dom/client";
import VideoPortal from "./VideoPortal.jsx";
import "../vendor/omnibioai-design-tokens/tokens.css";
import "../portal/portal.css";

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <VideoPortal />
  </React.StrictMode>,
);
