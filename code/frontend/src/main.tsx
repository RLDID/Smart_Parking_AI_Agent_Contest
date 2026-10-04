import { createRoot } from 'react-dom/client';
import { App } from './App';
import '../assets/parking-flow.css';
import '../assets/parking-fonts.css';
import '../assets/parking-glass.css';
import '../assets/parking-mobile.css';
import './app.css';
createRoot(document.getElementById('app')!).render(<App/>);
