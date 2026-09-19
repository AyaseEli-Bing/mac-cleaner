/** Tailwind 配置：语义色 token 与 MUI 主题保持一致（架构 10.4 节）。 */
export default {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        brand: '#4C8DF6',
        success: '#3FB950',
        warning: '#F5A623',
        danger: '#F2545B',
        ink: '#121212',
        card: '#1E1E1E',
        'card-light': '#FFFFFF',
        'ink-light': '#F5F5F5',
      },
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"PingFang SC"',
          '"Microsoft YaHei"',
          'Roboto',
          'sans-serif',
        ],
      },
      borderRadius: {
        xl: '14px',
      },
    },
  },
  plugins: [],
};
