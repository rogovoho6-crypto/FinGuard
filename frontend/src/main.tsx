import {createRoot} from 'react-dom/client';
import {Component, type ReactNode} from 'react';
import App from './App';
import './styles.css';
class ErrorBoundary extends Component<{children:ReactNode},{failed:boolean}> {state={failed:false}; static getDerivedStateFromError(){return {failed:true};} render(){return this.state.failed?<main className="boot"><h1>Не удалось открыть экран</h1><p>Обновите страницу. Сохранённые данные остаются на сервере.</p><button className="button" onClick={()=>window.location.reload()}>Обновить</button></main>:this.props.children;}}
createRoot(document.getElementById('root')!).render(<ErrorBoundary><App/></ErrorBoundary>);
