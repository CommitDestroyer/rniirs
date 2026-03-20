import React, { useState } from 'react';
import { Satellite, RefreshCw, Eye, MapPin } from 'lucide-react';

export default function ControlPanel() {
  const [lat, setLat] = useState('55.75');
  const [lng, setLng] = useState('37.61');

  return (
    <div className="flex flex-col gap-6 text-slate-100">
      {/* Шапка панели */}
      <div className="flex items-center gap-3 border-b border-slate-700/50 pb-4">
        <div className="bg-blue-600 p-2 rounded-lg">
          <Satellite className="w-5 h-5 text-white" />
        </div>
        <h1 className="text-lg font-bold tracking-wide">РНИИРС Монитор</h1>
      </div>

      <div className="space-y-6">
        {/* Секция 1: Управление данными */}
        <div className="flex flex-col gap-3">
          <h2 className="text-[11px] text-slate-400 uppercase tracking-widest font-bold">Управление данными</h2>
          
          <button className="w-full flex items-center justify-center gap-2 bg-blue-600 hover:bg-blue-500 transition-all rounded-lg py-2.5 text-sm font-medium shadow-lg shadow-blue-900/20 active:scale-95">
            <RefreshCw className="w-4 h-4" />
            Обновить TLE базу
          </button>
          
          <button className="w-full flex items-center justify-center gap-2 bg-slate-800/80 hover:bg-slate-700 border border-slate-600 transition-all rounded-lg py-2.5 text-sm font-medium active:scale-95">
            <Eye className="w-4 h-4 text-slate-300" />
            Отобразить орбиты
          </button>
        </div>

        {/* Секция 2: Наземная станция */}
        <div className="flex flex-col gap-3 pt-4 border-t border-slate-700/50">
          <h2 className="text-[11px] text-slate-400 uppercase tracking-widest font-bold">Наземная станция</h2>
          
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="text-xs text-slate-400 block mb-1.5 ml-1">Широта (Lat)</label>
              <input 
                type="text" 
                value={lat}
                onChange={(e) => setLat(e.target.value)}
                className="w-full bg-slate-950/50 border border-slate-700 rounded-lg p-2.5 text-sm focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all" 
              />
            </div>
            <div className="flex-1">
              <label className="text-xs text-slate-400 block mb-1.5 ml-1">Долгота (Lng)</label>
              <input 
                type="text" 
                value={lng}
                onChange={(e) => setLng(e.target.value)}
                className="w-full bg-slate-950/50 border border-slate-700 rounded-lg p-2.5 text-sm focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-all" 
              />
            </div>
          </div>
          
          <button className="w-full flex items-center justify-center gap-2 bg-emerald-600 hover:bg-emerald-500 transition-all rounded-lg py-2.5 mt-1 text-sm font-medium shadow-lg shadow-emerald-900/20 active:scale-95">
            <MapPin className="w-4 h-4" />
            Рассчитать видимость
          </button>
        </div>
      </div>
    </div>
  );
}
