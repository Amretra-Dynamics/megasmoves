import CurrentManual from "@/components/CurrentManual";
import Header from "@/components/Header";
import Footer from "@/components/Footer";
export const metadata={title:"Release availability - Megas Moves"};
export default function Page(){return <div className="site-surface"><Header/><main className="marketing-manual"><CurrentManual guide="availability"/></main><Footer/></div>;}
