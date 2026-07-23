const WHATSAPP_NUMBER = "966565743537";
const SUPPORT_MESSAGE = "Do you need any support?";

export function WhatsAppButton() {
  return (
    <a
      href={`https://wa.me/${WHATSAPP_NUMBER}`}
      target="_blank"
      rel="noopener noreferrer"
      title={SUPPORT_MESSAGE}
      aria-label={SUPPORT_MESSAGE}
      className="fixed bottom-5 right-5 z-50 flex h-14 w-14 items-center justify-center rounded-full bg-[#25D366] shadow-lg transition-transform hover:scale-105"
    >
      <svg
        viewBox="0 0 32 32"
        className="h-8 w-8 fill-white"
        aria-hidden="true"
      >
        <path d="M16.004 3C9.373 3 4 8.373 4 15.004c0 2.386.652 4.68 1.887 6.687L4 29l7.485-1.955a11.94 11.94 0 0 0 4.519.897h.005c6.63 0 12.004-5.373 12.004-12.004C28.013 8.373 22.64 3 16.004 3zm7.03 17.06c-.297.837-1.47 1.53-2.408 1.73-.64.135-1.475.244-4.287-.92-3.598-1.49-5.914-5.14-6.096-5.38-.176-.24-1.452-1.933-1.452-3.687 0-1.753.917-2.615 1.243-2.973.297-.324.65-.406.867-.406.217 0 .434.002.624.011.2.01.469-.076.734.56.297.717 1.008 2.47 1.096 2.65.088.18.146.39.03.626-.117.234-.176.38-.35.585-.176.205-.37.457-.528.615-.176.176-.36.366-.155.72.205.353.913 1.508 1.96 2.443 1.347 1.202 2.483 1.575 2.837 1.75.353.176.56.147.767-.088.205-.234.88-1.026 1.114-1.38.235-.352.47-.293.792-.176.322.117 2.044.964 2.395 1.14.352.176.586.264.674.41.088.147.088.85-.21 1.686z" />
      </svg>
    </a>
  );
}
