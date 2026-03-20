import { useEffect, useRef, useState } from 'react'
import Globe from 'globe.gl'
import { SatelliteUI } from './components/SatsInterface'
import { Rocket, BookOpen } from 'lucide-react'

export default function App() {
  const globeRef = useRef(null)
  const [isStarted, setIsStarted] = useState(false)

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

  return (
    <div className="relative w-screen h-screen overflow-hidden bg-black m-0 p-0">
      {/* Контейнер глобуса. Занимает весь экран. Рендерится всегда на фоне */}
      <div ref={globeRef} className="absolute inset-0 z-0" />
      
      {/* Если нажали "старт" -> показываем интерфейс управления глобусом */}
      {isStarted && <SatelliteUI />}

      {/* Стартовое меню (Welcome Screen), скрывается при isStarted === true */}
      {!isStarted && (
        <div className="absolute inset-0 z-50 flex flex-col items-center justify-center bg-slate-950/80 backdrop-blur-xl transition-opacity duration-500">
          <div className="flex flex-col items-center gap-10 bg-slate-900/60 p-12 rounded-[2rem] border border-slate-700/50 shadow-2xl">
            <div className="text-center space-y-4">
              <div className="inline-flex items-center justify-center w-20 h-20 rounded-full bg-blue-600/20 mb-4">
                <Globe className="w-10 h-10 text-blue-400" />
              </div>
              <h1 className="text-5xl font-black text-white tracking-widest uppercase">
                Монитор Спутников
              </h1>
              <p className="text-slate-400 text-lg font-medium tracking-wide">
                Интерактивная 3D-платформа • Кейс РНИИРС
              </p>
            </div>
            
            {/* Сгруппированные кнопки */}
            <div className="flex flex-col gap-4 w-full max-w-sm mt-4">
              <button 
                onClick={() => setIsStarted(true)}
                className="group relative flex items-center justify-center gap-3 w-full bg-blue-600 hover:bg-blue-500 text-white font-bold py-4 rounded-xl transition-all active:scale-95 shadow-lg shadow-blue-500/25 overflow-hidden"
              >
                <div className="absolute inset-0 w-full h-full bg-gradient-to-r from-transparent via-white/20 to-transparent -translate-x-full group-hover:animate-[shimmer_1.5s_infinite]"></div>
                <Rocket className="w-6 h-6" />
                <span>Запустить систему</span>
              </button>
              
              <button className="flex items-center justify-center gap-2 w-full bg-slate-800/80 hover:bg-slate-700 text-slate-300 font-medium py-3 rounded-xl transition-all border border-slate-700 active:scale-95">
                <BookOpen className="w-5 h-5" />
                <span>Документация</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
