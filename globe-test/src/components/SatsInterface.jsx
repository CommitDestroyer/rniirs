"use client"

import { useState, useRef, useEffect } from "react"
import { Search, Filter, Info, HelpCircle, Plus, Minus, Globe, Map, Check, Sun, Moon } from "lucide-react"

const satelliteFilters = [
  { id: "glonass", label: "ГЛОНАСС", color: "bg-cyan-400" },
  { id: "starlink", label: "Starlink", color: "bg-amber-400" },
  { id: "iss", label: "МКС", color: "bg-emerald-400" },
  { id: "weather", label: "Погода", color: "bg-rose-400" },
]

export function SatelliteUI() {
  const [searchQuery, setSearchQuery] = useState("")
  const [activeFilters, setActiveFilters] = useState(
    satelliteFilters.map((f) => f.id)
  )
  const [mapMode, setMapMode] = useState("realistic")
  const [zoom, setZoom] = useState(1)
  const [darkMode, setDarkMode] = useState(true)
  
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)
  const [infoOpen, setInfoOpen] = useState(false)

  const filtersRef = useRef(null)
  const helpRef = useRef(null)
  const infoRef = useRef(null)

  useEffect(() => {
    const handleClickOutside = (event) => {
      if (filtersRef.current && !filtersRef.current.contains(event.target)) {
        setFiltersOpen(false)
      }
      if (helpRef.current && !helpRef.current.contains(event.target)) {
        setHelpOpen(false)
      }
      if (infoRef.current && !infoRef.current.contains(event.target)) {
        setInfoOpen(false)
      }
    }

    document.addEventListener("mousedown", handleClickOutside)
    return () => document.removeEventListener("mousedown", handleClickOutside)
  }, [])

  const toggleFilter = (filterId) => {
    setActiveFilters((prev) =>
      prev.includes(filterId)
        ? prev.filter((id) => id !== filterId)
        : [...prev, filterId]
    )
  }

  const handleZoomIn = () => {
    setZoom((prev) => Math.min(prev + 0.2, 3))
  }

  const handleZoomOut = () => {
    setZoom((prev) => Math.max(prev - 0.2, 0.5))
  }

  // Theme-based styles
  const panelBg = darkMode 
    ? "border-white/10 bg-slate-900/80" 
    : "border-black/10 bg-white/80"
  
  const panelBgSolid = darkMode 
    ? "border-white/10 bg-slate-900/95" 
    : "border-black/10 bg-white/95"
  
  const textPrimary = darkMode ? "text-white" : "text-slate-900"
  const textSecondary = darkMode ? "text-slate-300" : "text-slate-600"
  const textMuted = darkMode ? "text-slate-400" : "text-slate-500"
  const textPlaceholder = darkMode ? "placeholder:text-slate-500" : "placeholder:text-slate-400"
  
  const borderColor = darkMode ? "border-white/10" : "border-black/10"
  
  const buttonHover = darkMode 
    ? "hover:bg-white/10 hover:text-white" 
    : "hover:bg-black/5 hover:text-slate-900"
  
  const activeButton = darkMode 
    ? "bg-cyan-500/20 text-cyan-400" 
    : "bg-cyan-500/20 text-cyan-600"
  
  const inactiveButton = darkMode 
    ? "text-slate-400 hover:bg-white/5 hover:text-slate-200" 
    : "text-slate-500 hover:bg-black/5 hover:text-slate-700"

  const checkboxActive = darkMode 
    ? "border-cyan-500 bg-cyan-500" 
    : "border-cyan-600 bg-cyan-600"
  
  const checkboxInactive = darkMode 
    ? "border-slate-600 bg-transparent" 
    : "border-slate-400 bg-transparent"

  const itemHover = darkMode ? "hover:bg-white/5" : "hover:bg-black/5"

  return (
    <div className="pointer-events-none fixed inset-0 z-50">
      {/* TOP LEFT: Search and Filters */}
      <div className="pointer-events-auto absolute left-4 top-4 flex items-center gap-2">
        <div className={`flex items-center gap-2 rounded-lg border ${panelBg} p-1 backdrop-blur-xl`}>
          <div className="relative">
            <Search className={`absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 ${textMuted}`} />
            <input
              type="text"
              placeholder="Поиск спутника..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className={`h-9 w-56 rounded-md border-0 bg-transparent pl-9 pr-3 text-sm ${textPrimary} ${textPlaceholder} outline-none focus:ring-0`}
            />
          </div>
          <div ref={filtersRef} className="relative">
            <button
              onClick={() => setFiltersOpen(!filtersOpen)}
              className={`flex h-9 items-center gap-2 border-l ${borderColor} px-3 ${textSecondary} transition-colors ${buttonHover}`}
            >
              <Filter className="h-4 w-4" />
              <span className="text-sm">Фильтры</span>
            </button>
            {filtersOpen && (
              <div className={`absolute left-0 top-full z-50 mt-2 w-56 rounded-lg border ${panelBgSolid} p-3 backdrop-blur-xl shadow-xl`}>
                <p className={`mb-3 text-xs font-medium uppercase tracking-wider ${textMuted}`}>
                  Типы спутников
                </p>
                <div className="space-y-1">
                  {satelliteFilters.map((filter) => (
                    <label
                      key={filter.id}
                      className={`flex cursor-pointer items-center gap-3 rounded-md p-2 transition-colors ${itemHover}`}
                    >
                      <div
                        onClick={() => toggleFilter(filter.id)}
                        className={`flex h-4 w-4 items-center justify-center rounded border transition-colors ${
                          activeFilters.includes(filter.id)
                            ? checkboxActive
                            : checkboxInactive
                        }`}
                      >
                        {activeFilters.includes(filter.id) && (
                          <Check className="h-3 w-3 text-white" />
                        )}
                      </div>
                      <div className={`h-2.5 w-2.5 rounded-full ${filter.color}`} />
                      <span className={`flex-1 text-sm ${textSecondary}`}>
                        {filter.label}
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* TOP RIGHT: Info and Help */}
      <div className="pointer-events-auto absolute right-4 top-4 flex items-center gap-2">
        <div ref={helpRef} className="relative">
          <button
            onClick={() => setHelpOpen(!helpOpen)}
            className={`flex h-10 w-10 items-center justify-center rounded-lg border ${panelBg} ${textSecondary} backdrop-blur-xl transition-colors ${buttonHover}`}
          >
            <HelpCircle className="h-5 w-5" />
            <span className="sr-only">Помощь</span>
          </button>
          {helpOpen && (
            <div className={`absolute right-0 top-full z-50 mt-2 w-72 rounded-lg border ${panelBgSolid} p-4 backdrop-blur-xl shadow-xl`}>
              <h3 className={`mb-3 text-sm font-semibold ${textPrimary}`}>Легенда карты</h3>
              <div className="space-y-3">
                <div className="flex items-center gap-3">
                  <div className="h-3 w-3 rounded-full bg-cyan-400 shadow-lg shadow-cyan-400/50" />
                  <span className={`text-sm ${textSecondary}`}>ГЛОНАСС — навигация</span>
                </div>
                <div className="flex items-center gap-3">
                  <div className="h-3 w-3 rounded-full bg-amber-400 shadow-lg shadow-amber-400/50" />
                  <span className={`text-sm ${textSecondary}`}>Starlink — интернет</span>
                </div>
                <div className="flex items-center gap-3">
                  <div className="h-3 w-3 rounded-full bg-emerald-400 shadow-lg shadow-emerald-400/50" />
                  <span className={`text-sm ${textSecondary}`}>МКС — станция</span>
                </div>
                <div className="flex items-center gap-3">
                  <div className="h-3 w-3 rounded-full bg-rose-400 shadow-lg shadow-rose-400/50" />
                  <span className={`text-sm ${textSecondary}`}>Погода — метеоспутники</span>
                </div>
              </div>
              <div className={`mt-4 border-t ${borderColor} pt-3`}>
                <p className={`text-xs ${textMuted}`}>
                  Нажмите на спутник для подробной информации. Используйте фильтры для отображения нужных категорий.
                </p>
              </div>
            </div>
          )}
        </div>

        <div ref={infoRef} className="relative">
          <button
            onClick={() => setInfoOpen(!infoOpen)}
            className={`flex h-10 w-10 items-center justify-center rounded-lg border ${panelBg} ${textSecondary} backdrop-blur-xl transition-colors ${buttonHover}`}
          >
            <Info className="h-5 w-5" />
            <span className="sr-only">О программе</span>
          </button>
          {infoOpen && (
            <div className={`absolute right-0 top-full z-50 mt-2 w-72 rounded-lg border ${panelBgSolid} p-4 backdrop-blur-xl shadow-xl`}>
              <h3 className={`mb-3 text-sm font-semibold ${textPrimary}`}>О программе</h3>
              <div className={`space-y-3 text-sm ${textSecondary}`}>
                <p>
                  Веб-приложение «Спутники» является 3D-картой объектов на орбите Земли в реальном времени.
                </p>
                <p>Автор приложения Вася</p>
                <p className="text-cyan-400">Контакты: 123.com</p>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* BOTTOM LEFT: Dark Mode Toggle */}
      <div className="pointer-events-auto absolute bottom-4 left-4">
        <button
          onClick={() => setDarkMode(!darkMode)}
          className={`flex h-10 w-10 items-center justify-center rounded-lg border ${panelBg} ${textSecondary} backdrop-blur-xl transition-colors ${buttonHover}`}
        >
          {darkMode ? (
            <Sun className="h-5 w-5" />
          ) : (
            <Moon className="h-5 w-5" />
          )}
          <span className="sr-only">{darkMode ? "Светлый режим" : "Темный режим"}</span>
        </button>
      </div>

      {/* BOTTOM RIGHT: Map Toggle and Zoom */}
      <div className="pointer-events-auto absolute bottom-4 right-4 flex flex-col items-end gap-3">
        {/* Zoom Controls */}
        <div className={`flex flex-col overflow-hidden rounded-lg border ${panelBg} backdrop-blur-xl`}>
          <button
            onClick={handleZoomIn}
            className={`flex h-10 w-10 items-center justify-center border-b ${borderColor} ${textSecondary} transition-all ${buttonHover} active:scale-95`}
          >
            <Plus className="h-5 w-5" />
            <span className="sr-only">Приблизить</span>
          </button>
          <button
            onClick={handleZoomOut}
            className={`flex h-10 w-10 items-center justify-center ${textSecondary} transition-all ${buttonHover} active:scale-95`}
          >
            <Minus className="h-5 w-5" />
            <span className="sr-only">Отдалить</span>
          </button>
        </div>

        {/* Map Mode Toggle */}
        <div className={`flex overflow-hidden rounded-lg border ${panelBg} backdrop-blur-xl`}>
          <button
            onClick={() => setMapMode("realistic")}
            className={`flex h-10 items-center gap-2 px-3 transition-colors ${
              mapMode === "realistic" ? activeButton : inactiveButton
            }`}
          >
            <Globe className="h-4 w-4" />
            <span className="text-xs">Реалистичная</span>
          </button>
          <button
            onClick={() => setMapMode("vector")}
            className={`flex h-10 items-center gap-2 border-l ${borderColor} px-3 transition-colors ${
              mapMode === "vector" ? activeButton : inactiveButton
            }`}
          >
            <Map className="h-4 w-4" />
            <span className="text-xs">Векторная</span>
          </button>
        </div>
      </div>
    </div>
  )
}
