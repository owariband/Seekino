import QtQuick

Canvas {
    id: item
    property string name: ""
    property color ink: Studio.muted
    implicitWidth: 18
    implicitHeight: 18
    onNameChanged: requestPaint()
    onInkChanged: requestPaint()
    onPaint: {
        var c = getContext("2d")
        c.reset()
        c.scale(width/24, height/24)
        c.lineWidth = 1.65; c.lineJoin = "round"; c.lineCap = "round"
        c.strokeStyle = ink; c.fillStyle = ink
        function path(points, fill) {
            c.beginPath(); c.moveTo(points[0],points[1])
            for (var i = 2; i < points.length; i += 2) c.lineTo(points[i],points[i+1])
            if (fill) { c.closePath(); c.fill() } else c.stroke()
        }
        function round(x,y,w,h,r) { c.beginPath(); c.roundedRect(x,y,w,h,r,r); c.stroke() }
        function circle(x,y,r,fill) {
            c.beginPath(); c.arc(x,y,r,0,2*Math.PI); if(fill)c.fill(); else c.stroke()
        }
        switch(name) {
        case "sidebar": round(2.5,4.5,19,15,2.5); path([9.2,5,9.2,19]); break
        case "search": circle(10.2,10.3,6.5); path([15,15.3,21,21]); break
        case "folder": path([2.5,7.5,2.5,5.7,3.5,4.8,9.5,4.8,12,7.5,20.5,7.5,21.5,8.6,21.5,18.4,20.5,19.4,3.6,19.4,2.5,18.3,2.5,7.5]); break
        case "film": round(3,4.5,18,15,2); path([7,5,7,19]); path([17,5,17,19]); for(var y=9;y<19;y+=5) {path([3,y,7,y]); path([17,y,21,y])} break
        case "library": path([4,5,4,19,7,19,7,5,4,5]); path([10,5,10,19,13,19,13,5,10,5]); path([16,6,20,19,22,18.5,18,5.5,16,6]); break
        case "chevron": path([9,6,15,12,9,18]); break
        case "left": path([14.5,5,7.5,12,14.5,19]); break
        case "down": path([6,9,12,15,18,9]); break
        case "plus": path([12,4.5,12,19.5]); path([4.5,12,19.5,12]); break
        case "activity": round(3,4,18,16,2); path([6,14,9,14,11,8,14,16,16,11,18,11]); break
        case "more": for(var x=5;x<=19;x+=7)circle(x,12,1.4,true); break
        case "import": path([12,3,12,14,7.5,9.5]); path([12,14,16.5,9.5]); path([4,13,4,19,5,20,19,20,20,19,20,13]); break
        case "play": path([7.5,4.8,19,12,7.5,19.2],true); break
        case "chat": path([3,4.5,21,4.5,21,16,10,16,5,20,5,16,3,16,3,4.5]); break
        case "history":
            c.beginPath(); c.arc(12.5,12,8.5,-2.45,2.55); c.stroke()
            path([3.5,4.5,3.5,9.5,8.5,9.5]); path([12.5,7.5,12.5,12,16,14]); break
        case "stop": round(6,6,12,12,2); break
        case "pause": round(6.5,5,2.5,14,0.8); round(15,5,2.5,14,.8); break
        case "volume": case "muted":
            path([3,9.3,7,9.3,12,5.2,12,18.8,7,14.7,3,14.7,3,9.3])
            if(name === "volume") { c.beginPath(); c.arc(11,12,6,-.85,.85); c.stroke(); c.beginPath(); c.arc(11,12,9.8,-.74,.74); c.stroke() }
            else {path([16,9,21,15]); path([21,9,16,15])} break
        case "fullscreen":
            path([9,4,4,4,4,9]); path([15,4,20,4,20,9])
            path([4,15,4,20,9,20]); path([15,20,20,20,20,15]); break
        case "exitFullscreen":
            path([4,9,9,9,9,4]); path([15,4,15,9,20,9])
            path([4,15,9,15,9,20]); path([15,20,15,15,20,15]); break
        case "dockRight": round(2.5,4.5,19,15,2.5); path([14.5,5,14.5,19]); break
        case "floatWindow": round(3,4,18,15,2.5); round(10,10,8,6,1); break
        case "close": path([6,6,18,18]); path([18,6,6,18]); break
        case "up": path([12,20,12,4.5,5,11.5]); path([12,4.5,19,11.5]); break
        case "sliders": path([3,7,7,7]); path([11,7,21,7]); circle(9,7,2); path([3,17,13,17]); path([17,17,21,17]); circle(15,17,2); break
        case "arrow": path([4,12,19,12,13,6]); path([19,12,13,18]); break
        case "check": path([4.5,12,9.5,17,20,6.5]); break
        case "help":
            circle(12,12,9)
            path([9.6,9.5,10.2,8.6,11.4,8,12.9,8.2,13.8,9.4,13.6,10.8,12,12,12,14])
            circle(12,17.5,1.1,true); break
        }
    }
}
