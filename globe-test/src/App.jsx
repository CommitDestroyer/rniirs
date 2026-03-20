import { useEffect, useRef } from 'react'
import Globe from 'globe.gl'

export default function App() {
  const globeRef = useRef(null)

  useEffect(() => {
    if (!globeRef.current) return

    const globe = Globe()(globeRef.current)
      .globeImageUrl('//unpkg.com/three-globe/example/img/earth-blue-marble.jpg')
      .backgroundImageUrl('//unpkg.com/three-globe/example/img/night-sky.png')

    globe.pointOfView(
      { lat: 55.7558, lng: 37.6173, altitude: 2.5 },
      1000
    )

    globe
      .pointsData([
        { lat: 55.7558, lng: 37.6173, size: 0.3, color: 'red' }
      ])
      .pointAltitude('size')
      .pointColor('color')

    return () => {
      if (globeRef.current) globeRef.current.innerHTML = ''
    }
  }, [])

  return <div ref={globeRef} style={{ width: '100vw', height: '100vh' }} />
}
