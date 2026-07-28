import React, { useState, useEffect } from "react";
import { RationalTime, PROXY_480P } from "@aether/contracts";

interface CanvasPreviewProps {
  currentTime: RationalTime;
  onTimeChange: (time: RationalTime) => void;
  timelineDuration: RationalTime;
}

export const CanvasPreview: React.FC<CanvasPreviewProps> = ({
  currentTime,
  onTimeChange,
  timelineDuration,
}) => {
  const [isPlaying, setIsPlaying] = useState(false);

  useEffect(() => {
    let interval: any;
    if (isPlaying) {
      interval = setInterval(() => {
        // Advance current time by 1 frame (1/24 seconds)
        const frameTime = new RationalTime(1000, 24000); // 1/24 seconds
        const nextTime = currentTime.add(frameTime);
        if (nextTime.greaterThan(timelineDuration) && timelineDuration.toSeconds() > 0) {
          setIsPlaying(false);
          onTimeChange(timelineDuration);
        } else {
          onTimeChange(nextTime);
        }
      }, 41.67); // ~24 fps
    }
    return () => clearInterval(interval);
  }, [isPlaying, currentTime, timelineDuration, onTimeChange]);

  const handlePlayPause = () => {
    setIsPlaying(!isPlaying);
  };

  const handleRewind = () => {
    setIsPlaying(false);
    onTimeChange(new RationalTime(0, 24000));
  };

  const currentSeconds = currentTime.toSeconds();
  const totalSeconds = timelineDuration.toSeconds();

  return (
    <div className="panel" style={{ flex: 1, height: "100%", background: "#0c0c0e", borderRight: "none" }}>
      <div className="panel-header">Canvas Monitor (480p Proxy Target)</div>
      <div className="canvas-panel" style={{ height: "100%", justifyContent: "space-between" }}>

        {/* Render Viewport */}
        <div className="canvas-viewport" style={{ flex: 1, width: "100%" }}>
          {/* Subtitle / Styling placeholder overlay */}
          <div style={{ color: "#10b981", fontSize: "12px", position: "absolute", top: "12px", left: "12px", background: "rgba(0,0,0,0.6)", padding: "4px 8px", borderRadius: "4px" }}>
            {PROXY_480P.width}x{PROXY_480P.height} @ {PROXY_480P.fps}fps ({PROXY_480P.codec.toUpperCase()})
          </div>

          <div style={{ textAlign: "center", pointerEvents: "none" }}>
            <div style={{ fontSize: "16px", fontWeight: "bold", color: "#818cf8" }}>AETHER WORKBENCH PREVIEW</div>
            <div style={{ fontSize: "11px", color: "#a1a1aa", marginTop: "4px" }}>
              Time: {currentSeconds.toFixed(3)}s / Frame {Math.round(currentSeconds * 24)}
            </div>
            {isPlaying && (
              <div style={{ fontSize: "12px", color: "#10b981", marginTop: "12px", fontWeight: "bold", animation: "pulse 1s infinite" }}>
                ● PLAYING
              </div>
            )}
          </div>

          <div style={{ position: "absolute", bottom: "16px", width: "100%", textAlign: "center", color: "#fff", textShadow: "1px 1px 2px #000" }}>
            {/* Live mockup overlay */}
            [ Aether Studio AI Preview Canvas ]
          </div>
        </div>

        {/* Timeline Slider & Navigation */}
        <div style={{ width: "100%", marginTop: "16px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "10px", width: "100%" }}>
            <input
              type="range"
              min={0}
              max={Math.max(totalSeconds, 1)}
              step={0.0416}
              value={currentSeconds}
              onChange={(e) => {
                const sec = parseFloat(e.target.value);
                onTimeChange(RationalTime.fromSeconds(sec, 24000));
              }}
              style={{ flex: 1 }}
            />
          </div>

          <div className="canvas-controls" style={{ margin: "12px 0 0 0" }}>
            <button className="secondary" onClick={handleRewind}>⏮ Rewind</button>
            <button onClick={handlePlayPause}>
              {isPlaying ? "⏸ Pause" : "▶ Play"}
            </button>

            <div style={{ fontSize: "12px", color: "#e4e4e7", display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "2px" }}>
              <div>{currentSeconds.toFixed(3)}s / {totalSeconds.toFixed(3)}s</div>
              <div style={{ fontSize: "10px", color: "#71717a" }}>
                Rational: ({currentTime.value} / {currentTime.timescale})
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>
  );
};
