"use client";

import { useState } from "react";
import axios from "axios";
import { LayoutDashboard, History, Stethoscope, BookOpen, BarChart3, Settings, Bell, Box, Code, FlaskConical, Server } from "lucide-react";
// Chart ekata awashya Recharts libraries tika import kireema
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell } from 'recharts';

export default function PipelineIQDashboard() {
  const [formData, setFormData] = useState({
    total_dependency_files_changed: 0,
    has_package_json_change: 0,
    has_lockfile_change: 0,
  });

  const [aiResult, setAiResult] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      const response = await axios.post("http://127.0.0.1:5000/predict", formData);
      setAiResult(response.data);
    } catch (error) {
      console.error("API Error:", error);
      alert("API ekata connect wenna bari wuna.");
    }
    setLoading(false);
  };

  return (
    <div className="flex h-screen bg-slate-50 font-sans text-slate-900">
      
      {/* Sidebar (Dark Blue) */}
      <div className="w-64 bg-[#1e293b] text-slate-300 flex flex-col justify-between">
        <div>
          <div className="p-6 flex items-center gap-3">
            <div className="bg-blue-500 p-2 rounded-lg text-white">
              <Box size={20} />
            </div>
            <div>
              <h1 className="text-white font-bold text-lg leading-tight">PipelineIQ</h1>
              <p className="text-[10px] text-slate-400">Explainable CI/CD AI</p>
            </div>
          </div>
          
          <nav className="mt-4">
            <a href="#" className="flex items-center gap-3 px-6 py-3 bg-[#334155] text-white border-l-4 border-blue-500">
              <LayoutDashboard size={18} /> Dashboard
            </a>
            <a href="#" className="flex items-center gap-3 px-6 py-3 hover:bg-[#334155] transition-colors">
              <History size={18} /> Pipeline History
            </a>
            <a href="#" className="flex items-center gap-3 px-6 py-3 hover:bg-[#334155] transition-colors">
              <Stethoscope size={18} /> Diagnosis
            </a>
            <a href="#" className="flex items-center gap-3 px-6 py-3 hover:bg-[#334155] transition-colors">
              <BookOpen size={18} /> Knowledge Base
            </a>
          </nav>
        </div>
      </div>

      {/* Main Content Area */}
      <div className="flex-1 overflow-auto">
        <header className="bg-white border-b border-slate-200 px-8 py-4 flex justify-between items-center">
          <div>
            <h2 className="text-2xl font-bold text-slate-800">Risk Dashboard</h2>
            <p className="text-sm text-slate-500">Pre-execution failure prediction for acme-api</p>
          </div>
        </header>

        <main className="p-8 max-w-6xl mx-auto">
          
          <h3 className="text-md font-semibold text-slate-700 mb-4">Predicted risk by expert category</h3>
          
          {/* Risk Cards */}
          <div className="grid grid-cols-4 gap-4 mb-10">
            {/* DEPENDENCY CARD (Connected to AI) */}
            <div className="bg-white rounded-xl border border-orange-200 p-5 shadow-sm relative overflow-hidden">
              <div className="flex justify-between items-start mb-4">
                <div className="flex items-center gap-2 text-orange-600 font-semibold">
                  <Box size={16} /> Dependency
                </div>
                <span className={`text-xs px-2 py-1 rounded-full font-medium ${aiResult?.status === 'Fail' ? 'bg-red-100 text-red-700' : 'bg-orange-100 text-orange-700'}`}>
                  {aiResult?.status === 'Fail' ? 'High Risk' : 'Medium'}
                </span>
              </div>
              <div className="flex items-center gap-4">
                <div className={`text-3xl font-bold ${aiResult?.status === 'Fail' ? 'text-red-600' : 'text-slate-800'}`}>
                  {aiResult ? aiResult.failure_probability : "34%"}
                </div>
              </div>
            </div>

            {/* DUMMY CARDS */}
            <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-sm opacity-60">
              <div className="flex items-center gap-2 text-purple-600 font-semibold mb-4"><Code size={16} /> Build</div>
              <div className="text-3xl font-bold text-slate-800">22%</div>
            </div>
            
            <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-sm opacity-60">
              <div className="flex items-center gap-2 text-teal-600 font-semibold mb-4"><FlaskConical size={16} /> Test</div>
              <div className="text-3xl font-bold text-slate-800">58%</div>
            </div>

            <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-sm opacity-60">
              <div className="flex items-center gap-2 text-orange-500 font-semibold mb-4"><Server size={16} /> Infra</div>
              <div className="text-3xl font-bold text-slate-800">12%</div>
            </div>
          </div>

          <div className="grid grid-cols-3 gap-6 mb-10">
            {/* AI Simulator Panel (Form) - Bam paththe */}
            <div className="col-span-1 bg-white border border-slate-200 rounded-xl p-6 shadow-sm">
              <h3 className="text-md font-semibold text-slate-700 mb-4 border-b border-slate-100 pb-2">AI Simulation</h3>
              <form onSubmit={handleSubmit} className="flex flex-col gap-4">
                <div>
                  <label className="block text-xs font-medium text-slate-500 mb-1">Total Files</label>
                  <input type="number" className="w-full border border-slate-300 rounded p-2 text-sm" value={formData.total_dependency_files_changed} onChange={(e) => setFormData({ ...formData, total_dependency_files_changed: parseInt(e.target.value) || 0 })} />
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-500 mb-1">package.json Change</label>
                  <select className="w-full border border-slate-300 rounded p-2 text-sm" value={formData.has_package_json_change} onChange={(e) => setFormData({ ...formData, has_package_json_change: parseInt(e.target.value) })}>
                    <option value={0}>0 - No</option><option value={1}>1 - Yes</option>
                  </select>
                </div>
                <div>
                  <label className="block text-xs font-medium text-slate-500 mb-1">Lockfile Change</label>
                  <select className="w-full border border-slate-300 rounded p-2 text-sm" value={formData.has_lockfile_change} onChange={(e) => setFormData({ ...formData, has_lockfile_change: parseInt(e.target.value) })}>
                    <option value={0}>0 - No</option><option value={1}>1 - Yes</option>
                  </select>
                </div>
                <button type="submit" disabled={loading} className="bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 px-6 rounded text-sm transition-colors mt-2">
                  {loading ? "Analyzing..." : "Run AI Prediction"}
                </button>
              </form>
            </div>

            {/* Explainability (SHAP) Chart - Dakunu paththe */}
            {aiResult && aiResult.shap_data ? (
              <div className="col-span-2 bg-white border border-slate-200 rounded-xl p-6 shadow-sm">
                <h3 className="text-md font-semibold text-slate-700 mb-1">Feature Impacts (Explainability)</h3>
                <p className="text-xs text-slate-500 mb-6">How each feature contributed to the failure risk prediction. <span className="text-red-500 font-semibold">Red</span> increases risk, <span className="text-emerald-500 font-semibold">Green</span> decreases risk.</p>
                
                <div className="h-52 w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={aiResult.shap_data} layout="vertical" margin={{ top: 5, right: 30, left: 30, bottom: 5 }}>
                      <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="#e2e8f0" />
                      <XAxis type="number" stroke="#94a3b8" fontSize={12} />
                      <YAxis dataKey="name" type="category" stroke="#64748b" fontSize={12} width={80} />
                      <Tooltip cursor={{fill: '#f8fafc'}} contentStyle={{borderRadius: '8px', border: '1px solid #e2e8f0'}} />
                      <Bar dataKey="impact" radius={[0, 4, 4, 0]} barSize={24}>
                        {aiResult.shap_data.map((entry: any, index: number) => (
                          <Cell key={`cell-${index}`} fill={entry.impact > 0 ? '#ef4444' : '#10b981'} />
                        ))}
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </div>
            ) : (
              <div className="col-span-2 bg-slate-50 border border-dashed border-slate-300 rounded-xl p-6 flex flex-col items-center justify-center text-slate-400">
                <BarChart3 size={32} className="mb-2 opacity-50" />
                <p className="text-sm">Run prediction to see AI decision drivers</p>
              </div>
            )}
          </div>

        </main>
      </div>
    </div>
  );
}