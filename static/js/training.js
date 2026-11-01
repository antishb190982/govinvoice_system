(function(){
  "use strict";
  function init(){
    var video=document.getElementById("tr-video");
    var buttons=Array.prototype.slice.call(document.querySelectorAll(".tr-chapter[data-start]"));
    var status=document.querySelector("[data-training-status]");
    if(!video||!buttons.length)return;
    function say(t){if(status)status.textContent=t;}
    function mark(){
      var t=video.currentTime,cur=null;
      buttons.forEach(function(b){var s=parseFloat(b.getAttribute("data-start"))||0;if(t+0.25>=s)cur=b;});
      buttons.forEach(function(b){var on=b===cur;b.classList.toggle("is-current",on);if(on)b.setAttribute("aria-current","step");else b.removeAttribute("aria-current");});
      if(cur&&!video.paused&&!video.ended){
        var message="Playing chapter: "+cur.querySelector(".tr-ch-t").firstChild.textContent.trim();
        if(status&&status.textContent!==message)say(message);
      }
    }
    video.addEventListener("error",function(event){
      if(event.target===video&&video.error)say("Video unavailable. Use the transcript and checklist below.");
    },true);
    video.addEventListener("loadedmetadata",function(){say("Ready. Select a chapter or press play.");mark();});
    video.addEventListener("playing",function(){
      var current=buttons.filter(function(b){return b.classList.contains("is-current");})[0];
      say(current?"Playing chapter: "+current.querySelector(".tr-ch-t").firstChild.textContent.trim():"Playing walkthrough.");
    });
    video.addEventListener("pause",function(){say("Paused. Use play or select another chapter.");});
    video.addEventListener("ended",function(){say("Walkthrough complete. Replay a chapter or use the checklist below.");});
    video.addEventListener("timeupdate",mark);
    buttons.forEach(function(b){
      b.addEventListener("click",function(){
        var s=parseFloat(b.getAttribute("data-start"))||0;
        function go(){
          try{video.currentTime=s;}catch(e){say("Unable to seek. Press play or use the transcript.");return;}
          mark();
          var p=video.play();
          if(p&&p.catch)p.catch(function(){say("Press play on the video to start this chapter.");});
        }
        if(video.readyState>=1)go();else{video.addEventListener("loadedmetadata",go,{once:true});video.load();}
        say("Loading chapter: "+b.querySelector(".tr-ch-t").firstChild.textContent.trim());
      });
    });
  }
  if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",init);else init();
})();
